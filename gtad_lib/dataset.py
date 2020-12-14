# -*- coding: utf-8 -*-
import numpy as np
import pandas as pd
import json
import torch.utils.data as data
import torch
import h5py
from torch.functional import F

def ioa_with_anchors(anchors_min, anchors_max, box_min, box_max):
    # calculate the overlap proportion between the anchor and all bbox for supervise signal,
    # the length of the anchor is 0.01
    len_anchors = anchors_max - anchors_min
    int_xmin = np.maximum(anchors_min, box_min)
    int_xmax = np.minimum(anchors_max, box_max)
    inter_len = np.maximum(int_xmax - int_xmin, 0.)
    scores = np.divide(inter_len, len_anchors)
    return scores


def iou_with_anchors(anchors_min, anchors_max, box_min, box_max):
    """Compute jaccard score between a box and the anchors.
    """
    len_anchors = anchors_max - anchors_min
    int_xmin = np.maximum(anchors_min, box_min)
    int_xmax = np.minimum(anchors_max, box_max)
    inter_len = np.maximum(int_xmax - int_xmin, 0.)
    union_len = len_anchors - inter_len + box_max - box_min
    # print inter_len,union_len
    jaccard = np.divide(inter_len, union_len)
    return jaccard


def load_json(file):
    with open(file) as json_file:
        json_data = json.load(json_file)
        return json_data


class VideoDataSet(data.Dataset):
    def __init__(self, opt, subset="train", mode="train"):
        self.temporal_scale = opt["temporal_scale"]  # 100
        self.temporal_gap = 1. / self.temporal_scale
        self.subset = subset
        self.mode = mode
        self.feature_path = opt["feature_path"]
        self.video_anno_path = opt["video_anno"]
        self._getDatasetDict()
        self._get_match_map()

    def _getDatasetDict(self):
        anno = load_json(self.video_anno_path)
        self.video_dict = {}
        class_list = []
        anno_database = anno['database']

        for vid in anno_database.keys():
            video_name = 'v_'+vid
            video_info = anno_database[vid]
            video_subset = anno_database[vid]['subset']
            if self.subset == "full":
                self.video_dict[video_name] = video_info
            if self.subset in video_subset:
                self.video_dict[video_name] = video_info
            for item in video_info['annotations']:
                class_list.append(item['label'])

        self.video_list = sorted(list(self.video_dict.keys()))
        print("%s subset video numbers: %d" % (self.subset, len(self.video_list)))

    def __getitem__(self, index):
        video_data = self._load_file(index)
        if self.mode == "train":
            match_score_start, match_score_end, confidence_score = self._get_train_label(index, self.anchor_xmin,
                                                                                         self.anchor_xmax)
            return video_data,confidence_score, match_score_start, match_score_end
        else:
            return index, video_data

    def _get_match_map(self):
        match_map = []
        for idx in range(self.temporal_scale):
            tmp_match_window = []
            xmin = self.temporal_gap * idx
            for jdx in range(1, self.temporal_scale + 1):
                xmax = xmin + self.temporal_gap * jdx
                tmp_match_window.append([xmin, xmax])
            match_map.append(tmp_match_window)
        match_map = np.array(match_map)  # 100x100x2
        match_map = np.transpose(match_map, [1, 0, 2])  # [0,1] [1,2] [2,3].....[99,100]
        match_map = np.reshape(match_map, [-1, 2])  # [0,2] [1,3] [2,4].....[99,101]   # duration x start
        self.match_map = match_map  # duration is same in row, start is same in col
        self.anchor_xmin = [self.temporal_gap * (i-0.5) for i in range(self.temporal_scale)]
        self.anchor_xmax = [self.temporal_gap * (i+0.5) for i in range(1, self.temporal_scale + 1)]

    def _load_file(self, index):
        video_name = self.video_list[index]
        
        if self.feature_path[-1]=='/':  # h5 files are in seperated files
            if ',' in self.feature_path: # concatenation of two
                feat = []
                for feature_path in self.feature_path.split(','):
                    with h5py.File(feature_path+video_name+'.h5', 'r') as f:
                        feat.append(f[video_name][:])
                feat = np.concatenate(feat,axis=1)
            elif 'I3D' in self.feature_path:
                folder_dict = {'train': 'training', 'validation': 'validation'}
                feat = np.load(self.feature_path+folder_dict[self.subset]+'/'+video_name[2:]+'.npy')
            else:
                # print(video_name, 'not found!!!!!!!!!!!')
                # feat = torch.randn((100,512))
                with h5py.File(self.feature_path+video_name+'.h5', 'r') as f:
                    feat = f[video_name][:]
        else:
            with h5py.File(self.feature_path, 'r') as features_h5:
                feat = features_h5[video_name][()]
        
        # video_data = torch.randn((100,2048))
        video_data = torch.Tensor(feat)
        video_data = torch.transpose(video_data, 0, 1)
        if video_data.shape[0]!=self.temporal_scale: # rescale to fixed shape
            video_data = F.interpolate(video_data.unsqueeze(0), size=self.temporal_scale, mode='linear',align_corners=False)[0,...]
        video_data.float()
        return video_data

    def _get_train_label(self, index, anchor_xmin, anchor_xmax):
        video_name = self.video_list[index]
        video_info = self.video_dict[video_name]
        # video_frame = video_info['duration_frame']
        # video_second = video_info['duration_second']
        # feature_frame = video_info['feature_frame']
        corrected_second = float(video_info['duration']) #float(feature_frame) / video_frame * video_second  # there are some frames not used
        video_labels = video_info['annotations']  # the measurement is second, not frame

        ##############################################################################################
        # change the measurement from second to percentage
        gt_bbox = []
        gt_iou_map = []
        for j in range(len(video_labels)):
            tmp_info = video_labels[j]
            tmp_start = max(min(1, tmp_info['segment'][0] / corrected_second), 0)
            tmp_end = max(min(1, tmp_info['segment'][1] / corrected_second), 0)
            gt_bbox.append([tmp_start, tmp_end])
            tmp_gt_iou_map = iou_with_anchors(
                self.match_map[:, 0], self.match_map[:, 1], tmp_start, tmp_end)
            tmp_gt_iou_map = np.reshape(tmp_gt_iou_map,
                                        [self.temporal_scale, self.temporal_scale])
            gt_iou_map.append(tmp_gt_iou_map)
        gt_iou_map = np.array(gt_iou_map)
        gt_iou_map = np.max(gt_iou_map, axis=0)
        gt_iou_map = torch.Tensor(gt_iou_map)
        ##############################################################################################

        ####################################################################################################
        # generate R_s and R_e
        gt_bbox = np.array(gt_bbox)
        gt_xmins = gt_bbox[:, 0]
        gt_xmaxs = gt_bbox[:, 1]
        gt_lens = gt_xmaxs - gt_xmins
        gt_len_small = 3 * self.temporal_gap  # np.maximum(self.temporal_gap, self.boundary_ratio * gt_lens)
        gt_start_bboxs = np.stack((gt_xmins - gt_len_small / 2, gt_xmins + gt_len_small / 2), axis=1)
        gt_end_bboxs = np.stack((gt_xmaxs - gt_len_small / 2, gt_xmaxs + gt_len_small / 2), axis=1)
        #####################################################################################################

        ##########################################################################################################
        # calculate the ioa for all timestamp
        match_score_start = []
        for jdx in range(len(anchor_xmin)):
            match_score_start.append(np.max(
                ioa_with_anchors(anchor_xmin[jdx], anchor_xmax[jdx], gt_start_bboxs[:, 0], gt_start_bboxs[:, 1])))
        match_score_end = []
        for jdx in range(len(anchor_xmin)):
            match_score_end.append(np.max(
                ioa_with_anchors(anchor_xmin[jdx], anchor_xmax[jdx], gt_end_bboxs[:, 0], gt_end_bboxs[:, 1])))
        match_score_start = torch.Tensor(match_score_start)
        match_score_end = torch.Tensor(match_score_end)
        ############################################################################################################

        return match_score_start, match_score_end, gt_iou_map

    def __len__(self):
        return len(self.video_list)


if __name__ == '__main__':
    from gtad_lib import opts

    opt = opts.parse_opt()
    opt = vars(opt)
    train_loader = torch.utils.data.DataLoader(VideoDataSet(opt, subset="train"),
                                               batch_size=opt["batch_size"], shuffle=True,
                                               num_workers=8, pin_memory=True)
    for a,b,c,d in train_loader:
        print(a.shape,b.shape,c.shape,d.shape)
        break
