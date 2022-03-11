import argparse
import os, sys
import shutil
import time
from pathlib import Path
import imageio

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(BASE_DIR)

print(sys.path)
import cv2
import torch
import torch.backends.cudnn as cudnn
from numpy import random
import scipy.special
import numpy as np
import torchvision.transforms as transforms
import PIL.Image as image
import matplotlib.pyplot as plt
from lib.utils import letterbox_for_img
from lib.config import cfg
from lib.config import update_config
from lib.utils.utils import create_logger, select_device, time_synchronized
from lib.models import get_net
from lib.dataset import LoadImages, LoadStreams
from lib.core.general import non_max_suppression, scale_coords
from lib.utils import plot_one_box,show_seg_result
from lib.core.function import AverageMeter
from lib.core.postprocess import morphological_process, connect_lane
normalize = transforms.Normalize(
        mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
    )

transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])

def detect_from_img(img, img_type='base64', show_lane=True, show_carbbox=True, show_deseg=True, cfg=cfg):

    # Initialize
    save_dir = 'inference/output'

    device = torch.device('cpu')
    model = get_net(cfg)
    checkpoint = torch.load('weights/End-to-end.pth', map_location=device)
    model.load_state_dict(checkpoint['state_dict'])
    model = model.to(device)

    # 读取img
    if img_type == 'base64':
        img0 = np.fromstring(img.decode('base64'), np.uint8)
    elif img_type == 'path':
        img0 = cv2.imread(img, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    else:
        img = img.copy()

    # image reshape
    h0, w0 = img0.shape[:2]
    img, ratio, pad = letterbox_for_img(img0, new_shape=640, auto=True)
    h, w = img.shape[:2]
    shapes = (h0, w0), ((h / h0, w / w0), pad)
    img = np.ascontiguousarray(img)

    raw_image = img0.copy()
    # 转tensor
    tensor_img = transform(img).to(device).float()
    # tensor_img = torch.from_numpy(img).permute(2, 0, 1).float().to(device)
    if tensor_img.ndimension() == 3:
        tensor_img = tensor_img.unsqueeze(0)

    # _ = model(tensor_img) # run once
    model.eval()

    det_out, da_seg_out,ll_seg_out= model(tensor_img)

    inf_out, _ = det_out
    det_pred = non_max_suppression(inf_out, conf_thres=0.25, iou_thres=0.45, classes=None, agnostic=False)

    det=det_pred[0]

    _, _, height, width = tensor_img.shape

    pad_w, pad_h = shapes[1][1]
    pad_w = int(pad_w)
    pad_h = int(pad_h)
    ratio = shapes[1][0][1]

    da_predict = da_seg_out[:, :, pad_h:(height-pad_h),pad_w:(width-pad_w)]
    da_seg_mask = torch.nn.functional.interpolate(da_predict, scale_factor=int(1/ratio), mode='bilinear')
    _, da_seg_mask = torch.max(da_seg_mask, 1)
    da_seg_mask = da_seg_mask.int().squeeze().cpu().numpy()

    ll_predict = ll_seg_out[:, :,pad_h:(height-pad_h),pad_w:(width-pad_w)]
    ll_seg_mask = torch.nn.functional.interpolate(ll_predict, scale_factor=int(1/ratio), mode='bilinear')
    _, ll_seg_mask = torch.max(ll_seg_mask, 1)
    ll_seg_mask = ll_seg_mask.int().squeeze().cpu().numpy()


    plt.imsave(save_dir+'/ll_seg_mask.jpg', ll_seg_mask)
    # da_seg_mask : 可行驶区域
    plt.imsave(save_dir+'/da_seg_mask.jpg', da_seg_mask)

    img_det = show_seg_result(raw_image, (da_seg_mask, ll_seg_mask), _, _, is_demo=True)
    # 车道线+可行驶区域 （此时img——det包含原图片和车道线信息）
    plt.imsave(save_dir+'/img_det.jpg', img_det)

    # 将img_det置换回去 让图片只包含车辆信息
    # img_det = img0
    if len(det):
        # Get names and colors
        names = model.module.names if hasattr(model, 'module') else model.names
        colors = [[random.randint(0, 255) for _ in range(3)] for _ in range(len(names))]
        det[:,:4] = scale_coords(tensor_img.shape[2:],det[:,:4],raw_image.shape).round()
        for *xyxy,conf,cls in reversed(det):
            label_det_pred = f'{names[int(cls)]} {conf:.2f}'
            plot_one_box(xyxy, img_det , label=label_det_pred, color=colors[int(cls)], line_thickness=2)
    plt.imsave(save_dir+'/car_bbox.jpg', img_det)

def detect(cfg,opt):

    logger, _, _ = create_logger(
        cfg, cfg.LOG_DIR, 'demo')

    device = select_device(logger,opt['device'])
    if os.path.exists(opt['save_dir']):  # output dir
        shutil.rmtree(opt['save_dir'])  # delete dir
    os.makedirs(opt['save_dir'])  # make new dir
    half = device.type != 'cpu'  # half precision only supported on CUDA

    # Load model
    model = get_net(cfg)
    checkpoint = torch.load(opt['weights'], map_location= device)
    model.load_state_dict(checkpoint['state_dict'])
    model = model.to(device)
    if half:
        model.half()  # to FP16

    # Set Dataloader
    if opt['source'].isnumeric():
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(opt['source'], img_size=opt['img_size'])
        bs = len(dataset)  # batch_size
    else:
        dataset = LoadImages(opt['source'], img_size=opt['img_size'])
        bs = 1  # batch_size


    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names
    colors = [[random.randint(0, 255) for _ in range(3)] for _ in range(len(names))]


    # Run inference
    t0 = time.time()

    vid_path, vid_writer = None, None
    img = torch.zeros((1, 3, opt['img_size'], opt['img_size']), device=device)  # init img
    _ = model(img.half() if half else img) if device.type != 'cpu' else None  # run once
    model.eval()

    inf_time = AverageMeter()
    nms_time = AverageMeter()
    
    for i, (path, img, img_det, vid_cap,shapes) in enumerate(dataset):
        img = transform(img).to(device)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        if img.ndimension() == 3:
            img = img.unsqueeze(0)
        # Inference
        t1 = time_synchronized()
        det_out, da_seg_out,ll_seg_out= model(img)


        t2 = time_synchronized()
        # if i == 0:
        #     print(det_out)
        inf_out, _ = det_out
        inf_time.update(t2-t1,img.size(0))

        # Apply NMS
        t3 = time_synchronized()
        det_pred = non_max_suppression(inf_out, conf_thres=opt['conf_thres'], iou_thres=opt['iou_thres'], classes=None, agnostic=False)
        t4 = time_synchronized()

        nms_time.update(t4-t3,img.size(0))
        det=det_pred[0]

        save_path = str(opt['save_dir'] +'/'+ Path(path).name) if dataset.mode != 'stream' else str(opt['save_dir'] + '/' + "web.mp4")

        _, _, height, width = img.shape
        h,w,_=img_det.shape
        raw_image = img_det.copy()

        pad_w, pad_h = shapes[1][1]
        pad_w = int(pad_w)
        pad_h = int(pad_h)
        ratio = shapes[1][0][1]

        da_predict = da_seg_out[:, :, pad_h:(height-pad_h),pad_w:(width-pad_w)]
        da_seg_mask = torch.nn.functional.interpolate(da_predict, scale_factor=int(1/ratio), mode='bilinear')
        _, da_seg_mask = torch.max(da_seg_mask, 1)
        da_seg_mask = da_seg_mask.int().squeeze().cpu().numpy()
        # da_seg_mask = morphological_process(da_seg_mask, kernel_size=7)

        
        ll_predict = ll_seg_out[:, :,pad_h:(height-pad_h),pad_w:(width-pad_w)]
        ll_seg_mask = torch.nn.functional.interpolate(ll_predict, scale_factor=int(1/ratio), mode='bilinear')
        _, ll_seg_mask = torch.max(ll_seg_mask, 1)
        ll_seg_mask = ll_seg_mask.int().squeeze().cpu().numpy()
        # Lane line post-processing
        #ll_seg_mask = morphological_process(ll_seg_mask, kernel_size=7, func_type=cv2.MORPH_OPEN)
        #ll_seg_mask = connect_lane(ll_seg_mask)

        img_det = show_seg_result(raw_image, (da_seg_mask, ll_seg_mask), _, _, is_demo=True)


        if len(det):
            det[:,:4] = scale_coords(img.shape[2:],det[:,:4],img_det.shape).round()
            for *xyxy,conf,cls in reversed(det):
                label_det_pred = f'{names[int(cls)]} {conf:.2f}'
                plot_one_box(xyxy, img_det , label=label_det_pred, color=colors[int(cls)], line_thickness=2)
        plt.imsave(opt['save_dir']+'/car_bbox.jpg', img_det)
        if dataset.mode == 'images':
            cv2.imwrite(save_path,img_det)

        elif dataset.mode == 'video':
            if vid_path != save_path:  # new video
                vid_path = save_path
                if isinstance(vid_writer, cv2.VideoWriter):
                    vid_writer.release()  # release previous video writer

                fourcc = 'mp4v'  # output video codec
                fps = vid_cap.get(cv2.CAP_PROP_FPS)
                h,w,_=img_det.shape
                vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
            vid_writer.write(img_det)
        
        else:
            cv2.imshow('image', img_det)
            cv2.waitKey(1)  # 1 millisecond

    print('Results saved to %s' % Path(opt['save_dir']))
    print('Done. (%.3fs)' % (time.time() - t0))
    print('inf : (%.4fs/frame)   nms : (%.4fs/frame)' % (inf_time.avg,nms_time.avg))




if __name__ == '__main__':
    opt = {
        'weights': 'weights/End-to-end.pth',
        'source': 'inference/images',
        'img_size': 640,
        'conf_thres': 0.25,
        'iou_thres': 0.45,
        'device': 'cpu',
        'save_dir': 'inference/output',
        'augment': False,
        'update': False
    }

    with torch.no_grad():
        # detect(cfg,opt)
        detect_from_img('inference/images/0ace96c3-48481887.jpg', img_type='path')
