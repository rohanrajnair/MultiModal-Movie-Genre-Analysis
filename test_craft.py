import sys
import os
import time
import argparse

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.autograd import Variable
import matplotlib.pyplot as plt
from PIL import Image
from collections import OrderedDict
sys.path.insert(0, 'CRAFT-pytorch')
from craft import CRAFT
import cv2
from skimage import io
import numpy as np
import craft_utils
import imgproc
import file_utils

sys.path.insert(0, 'deep-text-recognition-benchmark')
from model import Model
from utils import CTCLabelConverter, AttnLabelConverter
from dataset import RawDataset, AlignCollate
canvas_size = 1280
mag_ratio = 1.5
show_time = False
def copyStateDict(state_dict):
    if list(state_dict.keys())[0].startswith("module"):
        start_idx = 1
    else:
        start_idx = 0
    new_state_dict = OrderedDict()
    for k, v in state_dict.items():
        name = ".".join(k.split(".")[start_idx:])
        new_state_dict[name] = v
    return new_state_dict

def test_net(net, image, text_threshold, link_threshold, low_text, cuda, poly, refine_net=None):
    t0 = time.time()

    # resize
    img_resized, target_ratio, size_heatmap = imgproc.resize_aspect_ratio(image, canvas_size, interpolation=cv2.INTER_LINEAR, mag_ratio=mag_ratio)
    ratio_h = ratio_w = 1 / target_ratio

    # preprocessing
    x = imgproc.normalizeMeanVariance(img_resized)
    x = torch.from_numpy(x).permute(2, 0, 1)    # [h, w, c] to [c, h, w]
    x = Variable(x.unsqueeze(0))                # [c, h, w] to [b, c, h, w]
    if cuda:
        x = x.cuda()

    # forward pass
    with torch.no_grad():
        y, feature = net(x)

    # make score and link map
    score_text = y[0,:,:,0].cpu().data.numpy()
    score_link = y[0,:,:,1].cpu().data.numpy()

    # refine link
    if refine_net is not None:
        with torch.no_grad():
            y_refiner = refine_net(y, feature)
        score_link = y_refiner[0,:,:,0].cpu().data.numpy()

    t0 = time.time() - t0
    t1 = time.time()

    # Post-processing
    boxes, polys = craft_utils.getDetBoxes(score_text, score_link, text_threshold, link_threshold, low_text, poly)

    # coordinate adjustment
    boxes = craft_utils.adjustResultCoordinates(boxes, ratio_w, ratio_h)
    polys = craft_utils.adjustResultCoordinates(polys, ratio_w, ratio_h)
    for k in range(len(polys)):
        if polys[k] is None: polys[k] = boxes[k]

    t1 = time.time() - t1

    # render results (optional)
    render_img = score_text.copy()
    render_img = np.hstack((render_img, score_link))
    ret_score_text = imgproc.cvt2HeatmapImg(render_img)

    if show_time : print("\ninfer/postproc time : {:.3f}/{:.3f}".format(t0, t1))

    return boxes, polys, ret_score_text
if __name__ == '__main__':
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    cuda = torch.cuda.is_available()

    net = CRAFT()
    #NOT CUDA (for now)
    if cuda:
        net.load_state_dict(copyStateDict(torch.load('CRAFT-pytorch/craft_mlt_25k.pth')))
    else:
        net.load_state_dict(copyStateDict(torch.load('CRAFT-pytorch/craft_mlt_25k.pth', map_location='cpu')))
    
    if cuda:
        net = net.cuda()
        net = torch.nn.DataParallel(net)
        cudnn.benchmark = False
    net.eval()
    refine_net = None
    
    text_threshold = 0.7
    link_threshold = 0.4
    low_text = 0.4
    poly = False

    result_folder = './intermediate_result/'

    if not os.path.isdir(result_folder):
        os.mkdir(result_folder)

    
    #print(device)
    parser = argparse.ArgumentParser()
    #Data processing
    parser.add_argument('--batch_max_length', type=int, default=25, help='maximum-label-length')
    parser.add_argument('--imgH', type=int, default=32, help='the height of the input image')
    parser.add_argument('--imgW', type=int, default=100, help='the width of the input image')
    parser.add_argument('--rgb', default=False, action='store_true', help='use rgb input')
    parser.add_argument('--character', type=str, default='0123456789abcdefghijklmnopqrstuvwxyz', help='character label')
    parser.add_argument('--sensitive', action='store_true', help='for sensitive character mode')
    parser.add_argument('--PAD', action='store_true', help='whether to keep ratio then pad for image resize')
    #Model Architecture
    parser.add_argument('--Transformation', type=str, default='TPS', help='Transformation stage. None|TPS')
    parser.add_argument('--FeatureExtraction', type=str, default='ResNet', help='FeatureExtraction stage. VGG|RCNN|ResNet')
    parser.add_argument('--SequenceModeling', type=str, default='BiLSTM', help='SequenceModeling stage. None|BiLSTM')
    parser.add_argument('--Prediction', type=str, default='Attn', help='Prediction stage. CTC|Attn')
    parser.add_argument('--num_fiducial', type=int, default=20, help='number of fiducial points of TPS-STN')
    parser.add_argument('--input_channel', type=int, default=1, help='the number of input channel of Feature extractor')
    parser.add_argument('--output_channel', type=int, default=512, help='the number of output channel of Feature extractor')
    parser.add_argument('--hidden_size', type=int, default=256, help='the size of the LSTM hidden state')
    opt = parser.parse_args()

    if 'CTC' in opt.Prediction:
        converter = CTCLabelConverter(opt.character)
    else:
        converter = AttnLabelConverter(opt.character)
    opt.num_class = len(converter.character)
    #print(opt.rgb)
    if opt.rgb:
        opt.input_channel = 3
    opt.num_gpu = torch.cuda.device_count()
    opt.batch_size = 192
    opt.workers = 1
    model = Model(opt)
    model = torch.nn.DataParallel(model).to(device)
    model.load_state_dict(torch.load('deep-text-recognition-benchmark/TPS-ResNet-BiLSTM-Attn.pth', map_location=device))

    filename = "87"
    file_extension = ".jpg"
    image = imgproc.loadImage(filename+file_extension)
    print("Starting text extraction")
    bboxes, polys, score_text = test_net(net, image, text_threshold, link_threshold, low_text, cuda, poly, refine_net)
    print("Finished text extraction")
    #print(bboxes)
    #print(polys)
    img=cv2.imread(filename+file_extension)
    rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    plt.imshow(rgb_img)
    plt.show()
    points = []
    order = []
    for i in range(0,len(bboxes)):
        sample_bbox = bboxes[i]
        #print(bboxes[0])
        min_point = sample_bbox[0]
        max_point = sample_bbox[2]
        for j,p in enumerate(sample_bbox):
            if(p[0]<=min_point[0] and p[1]<=min_point[1]):
                min_point = p
                #print("min", j)
            if(p[0]>=max_point[0] and p[1]>=max_point[1]):
                max_point = p
                #print("max", j)
        points.append((min_point, max_point))
        order.append(0)
        
        #print("Cropped image")
        #print(min_point)
        #if(i==6):
        #    print(sample_bbox)
        #    print(min_point)
        #    print(max_point)
        #    crop_image = rgb_img[int(min_point[1]):int(max_point[1]),int(min_point[0]):int(max_point[0])]
        #    plt.imshow(crop_image)
        #    plt.show()
        #mask_file = result_folder + filename+"_" + str(i) + '.jpg'
        #cv2.imwrite(mask_file, crop_image)
    num_ordered = 0
    rows_ordered = 0
    points_sorted =[]
    ordered_points_index = 0
    order_sorted=[]
    while(num_ordered<len(points)):
        #find lowest-y that is unordered
        min_y = len(rgb_img)
        min_y_index = -1
        for i in range(0,len(points)):
            if(order[i]==0):
                if(points[i][0][1]<=min_y):
                    min_y = points[i][0][1]
                    min_y_index = i
        rows_ordered+=1
        order[min_y_index] = rows_ordered
        num_ordered+=1
        points_sorted.append(points[min_y_index])
        order_sorted.append(rows_ordered)
        ordered_points_index = len(points_sorted)-1
        
        # Group bboxes that are on the same row
        max_y = points[min_y_index][1][1]
        range_y = max_y-min_y
        for i in range(0,len(points)):
            if(order[i]==0):
                min_y_i = points[i][0][1]
                max_y_i = points[i][1][1]
                range_y_i = max_y_i-min_y_i
                if(max_y_i>=min_y and min_y_i<=max_y):
                    overlap = (min(max_y_i,max_y)-max(min_y_i,min_y))/(min(range_y,range_y_i))
                    if(overlap>=0.30):
                        order[i] = rows_ordered
                        num_ordered+=1
                        min_x_i = points[i][0][0]
                        for j in range(ordered_points_index, len(points_sorted)+1):
                            if(j<len(points_sorted)): #insert before
                                min_x_j = points_sorted[j][0][0]
                                if(min_x_i<min_x_j):
                                    points_sorted.insert(j, points[i])
                                    order_sorted.insert(j, rows_ordered)
                                    break
                            else: #insert at the end of array
                                points_sorted.insert(j, points[i])
                                order_sorted.insert(j, rows_ordered)
                                break
                        
    #print(len(points))
    #print(len(points_sorted))
    for i in range(0,len(points_sorted)):
        min_point = points_sorted[i][0]
        max_point = points_sorted[i][1]
        #print("Cropped image")
        mask_file = result_folder + filename+"_" + str(order_sorted[i])+"_"+str(i) + '.jpg'
        #print(mask_file)
        crop_image = rgb_img[int(min_point[1]):int(max_point[1]),int(min_point[0]):int(max_point[0])]
        #plt.imshow(crop_image)
        #plt.show()
        cv2.imwrite(mask_file, crop_image)


    # prepare data. two demo images from https://github.com/bgshih/crnn#run-demo
    #result_folder = './intermediate_result/'
    AlignCollate_demo = AlignCollate(imgH=opt.imgH, imgW=opt.imgW, keep_ratio_with_pad=opt.PAD)
    demo_data = RawDataset(root=result_folder, opt=opt)  # use RawDataset
    demo_loader = torch.utils.data.DataLoader(
        demo_data, batch_size=opt.batch_size,
        shuffle=False,
        num_workers=int(opt.workers),
        collate_fn=AlignCollate_demo, pin_memory=True)
    print("Starting text classification")
    model.eval()
    with torch.no_grad():
        for image_tensors, image_path_list in demo_loader:
            batch_size = image_tensors.size(0)
            image = image_tensors.to(device)
            #image = (torch.from_numpy(crop_image).unsqueeze(0)).to(device)
            #print(image_path_list)
            #print(image.size())
            length_for_pred = torch.IntTensor([opt.batch_max_length] * batch_size).to(device)
            text_for_pred = torch.LongTensor(batch_size, opt.batch_max_length + 1).fill_(0).to(device)
            preds = model(image, text_for_pred, is_train=False)
            _, preds_index = preds.max(2)
            preds_str = converter.decode(preds_index, length_for_pred)
            #print(preds_str)
            output_string = ""
            curr_order = 1
            for path,p in zip(image_path_list, preds_str):
                if 'Attn' in opt.Prediction:
                    pred_EOS = p.find('[s]')
                    p = p[:pred_EOS]  # prune after "end of sentence" token ([s])
                path_info = path[len(result_folder):-len(file_extension)].split("_")
                if(path_info[0]==filename):
                    if(int(path_info[1])>curr_order):
                        curr_order+=1
                        output_string+="\n"
                    output_string+=p+" "
            print(output_string)
    plt.imshow(rgb_img)
    plt.show()
    #print(opt)
