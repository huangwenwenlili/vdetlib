#!/usr/bin/env python
from ..utils.protocol import frame_path_at, track_box_at_frame, bbox_hash, \
    tubelets_overlap, tubelets_proto_from_tracks_proto, tubelet_box_at_frame, det_score
from ..utils.common import imread, svm_from_rcnn_model, iou
from ..utils.log import logging
from ..vdet.image_det import googlenet_det, googlenet_features, svm_scores
from ..vdet.dataset import imagenet_vdet_classes, index_vdet_to_det
import numpy as np
import sys
import os
import copy

def score_conv_cls(score_proto, net):
    new_score_proto = copy.copy(score_proto)
    print "{}: {} tubelet(s).".format(score_proto['video'], len(new_score_proto['tubelets']))
    for tubelet in new_score_proto['tubelets']:
        track = {}
        track['length'] = len(tubelet['boxes'])
        track['gt'] = tubelet['gt']
        track['mean_iou'] = np.mean([map(lambda x:x['gt_overlap'],
                                  tubelet['boxes'])])
        track['det_scores'] = map(lambda x:x['det_score'],
                                  tubelet['boxes'])
        track['track_scores'] = map(lambda x:x['track_score'],
                                  tubelet['boxes'])
        track['anchors'] = map(lambda x:x['anchor'] * 1. / track['length'],
                                  tubelet['boxes'])
        track['abs_anchors'] = map(abs, track['anchors'])
        track['gt_overlaps'] = map(lambda x:x['gt_overlap'],
                                  tubelet['boxes'])
        track['labels'] = [1 if iou >= 0.5 else 0 for iou in track['gt_overlaps']]

        # skip memory heavy features if possible
        if 'all_scores' in net.blobs.keys():
            track['all_scores'] = map(lambda x:x['all_score'],
                                  tubelet['boxes'])
        if 'feats' in net.blobs.keys():
            track['feats'] = map(lambda x:x['feat'],
                                  tubelet['boxes'])

        for blob_name in set(net.blobs.keys()).intersection(set(track.keys())):
            num_channels = net.blobs[blob_name].shape[1]
            net.blobs[blob_name].reshape(1, num_channels, 1, track['length'])
            net.blobs[blob_name].data[...] = np.asarray(track[blob_name], dtype='float32')
        blobs_out = net.forward()
        probs = blobs_out['probs'][:, 1,:]
        for box, prob in zip(tubelet['boxes'], probs.ravel()):
            box['conv_score'] = float(prob)
    return new_score_proto

def fast_rcnn_cls(video_proto, track_proto, net, class_idx):
    sys.path.insert(1, os.path.join(os.path.dirname(__file__),
        '../../External/fast-rcnn/lib/'))
    sys.path.insert(1, os.path.join(os.path.dirname(__file__),
        '../../External/fast-rcnn/caffe-fast-rcnn/python'))
    from fast_rcnn.test import im_detect
    new_tracks = [[] for _ in track_proto['tracks']]
    for frame in video_proto['frames']:
        frame_id = frame['frame']
        img = imread(frame_path_at(video_proto, frame_id))
        boxes = [track_box_at_frame(tracklet, frame_id) \
                 for tracklet in track_proto['tracks']]
        valid_boxes = np.asarray([box for box in boxes if box is not None])
        valid_index = [i for i in len(boxes) if boxes[i] is not None]
        scores, pred_boxes = im_detect(net, img, valid_boxes)
        for score, box, track_id in zip(scores, pred_boxes, valid_index):
            new_tracks[track_id].append(
                {
                    "frame": frame_id,
                    "bbox": list(box),
                    "score": score[class_idx],
                    "hash": bbox_hash(video_proto['video'], frame_id, box)
                })
    return new_tracks


def googlenet_cls(video_proto, track_proto, net, class_idx):
    new_tracks = [[] for _ in track_proto['tracks']]
    logging.info("Classifying {}...".format(video_proto['video']))
    for frame in video_proto['frames']:
        frame_id = frame['frame']
        img = imread(frame_path_at(video_proto, frame_id))
        boxes = [track_box_at_frame(tracklet, frame_id) \
                 for tracklet in track_proto['tracks']]
        valid_boxes = np.asarray([box for box in boxes if box is not None])
        valid_index = [i for i, box in enumerate(boxes) if box is not None]
        logging.info("frame {}: {} boxes".format(frame_id, len(valid_index)))
        for box, track_id in zip(valid_boxes, valid_index):
            scores = googlenet_det(img, box, net)
            new_tracks[track_id].append(
                {
                    "frame": frame_id,
                    "bbox": list(box),
                    "score": scores[class_idx],
                    "hash": bbox_hash(video_proto['video'], frame_id, box)
                })
    return new_tracks


def rcnn_scoring(vid_proto, track_proto, net, class_idx, rcnn_model,
        save_feat=False, save_all_sc=False):
    svm_model = svm_from_rcnn_model(rcnn_model)
    tubelets_proto = tubelets_proto_from_tracks_proto(track_proto['tracks'], class_idx)
    logging.info("Scoring {} for {}...".format(vid_proto['video'],
                 imagenet_vdet_classes[class_idx]))
    for frame in vid_proto['frames']:
        frame_id = frame['frame']
        img = imread(frame_path_at(vid_proto, frame_id))
        boxes = [tubelet_box_at_frame(tubelet, frame_id) \
                 for tubelet in tubelets_proto]
        valid_boxes = np.asarray([box for box in boxes if box is not None])
        valid_index = [i for i, box in enumerate(boxes) if box is not None]
        logging.info("frame {}: {} boxes".format(frame_id, len(valid_index)))
        if len(valid_index) == 0:
            continue
        features = googlenet_features(img, valid_boxes, net, 'pool5')
        scores = svm_scores(features, svm_model)
        if scores.shape[1] == 200:
            cls_scores = scores[:, index_vdet_to_det[class_idx] - 1]
        else:
            raise
        for score, tubelet_id, feat, all_score in \
                zip(cls_scores, valid_index, features, scores):
            cur_box = [box for box in tubelets_proto[tubelet_id]['boxes'] \
                if box['frame'] == frame_id]
            assert len(cur_box) == 1
            cur_box[0]['det_score'] = score
            if save_feat:
                cur_box[0]['feat'] = feat.ravel().tolist()
            if save_all_sc:
                cur_box[0]['all_score'] = all_score.ravel().tolist()
    return tubelets_proto

def sampling_boxes(orig_box, num, ratio = 0.05, return_orig=True):
    h, w = orig_box[3] - orig_box[1], orig_box[2] - orig_box[0]
    offsets = np.random.uniform(-ratio, ratio, [num, 4]) * [w, h, w, h]
    if not return_orig:
        return orig_box + offsets
    else:
        return np.vstack((orig_box, orig_box+offsets))

def rcnn_sampling_scoring(vid_proto, track_proto, net, class_idx, rcnn_model,
        samples_per_box = 32, ratio = 0.05,
        save_feat=False, save_all_sc=False):
    svm_model = svm_from_rcnn_model(rcnn_model)
    tubelets_proto = tubelets_proto_from_tracks_proto(track_proto['tracks'], class_idx)
    logging.info("Scoring {} for {}...".format(vid_proto['video'],
                 imagenet_vdet_classes[class_idx]))
    for frame in vid_proto['frames']:
        frame_id = frame['frame']
        img = imread(frame_path_at(vid_proto, frame_id))
        boxes = [tubelet_box_at_frame(tubelet, frame_id) \
                 for tubelet in tubelets_proto]
        valid_boxes = np.asarray([box for box in boxes if box is not None])
        valid_index = [i for i, box in enumerate(boxes) if box is not None]
        logging.info("frame {}: {} boxes".format(frame_id, len(valid_index)))
        if len(valid_index) == 0:
            continue

        # sample nearby boxes to increase spatial robustness
        sampled_boxes = np.vstack([sampling_boxes(box, samples_per_box, ratio) \
                for box in valid_boxes])
        features = googlenet_features(img, sampled_boxes, net, 'pool5')
        scores = svm_scores(features, svm_model)
        if scores.shape[1] == 200:
            cls_scores = scores[:, index_vdet_to_det[class_idx] - 1]
            cls_scores = cls_scores.reshape((len(valid_index), -1))
            max_scores = cls_scores.max(axis=1)
            # extract cooresponding features and all class scores
            max_idx = np.argmax(cls_scores, axis=1)
        else:
            raise

        # extract features and scores of box with maximum score
        features = features.reshape((len(valid_index), samples_per_box+1, -1))
        features = features[xrange(len(valid_index)), max_idx,:]
        scores = scores.reshape((len(valid_index), samples_per_box+1, -1))
        scores = scores[xrange(len(valid_index)), max_idx,:]
        sampled_boxes = sampled_boxes.reshape((len(valid_index), samples_per_box+1, -1))
        boxes = sampled_boxes[xrange(len(valid_index)), max_idx,:]
        for score, tubelet_id, feat, all_score, max_box in \
                zip(max_scores, valid_index, features, scores, boxes):
            cur_box = [box for box in tubelets_proto[tubelet_id]['boxes'] \
                if box['frame'] == frame_id]
            assert len(cur_box) == 1
            cur_box[0]['det_score'] = score
            cur_box[0]['bbox'] = max_box.tolist()
            if save_feat:
                cur_box[0]['feat'] = feat.ravel().tolist()
            if save_all_sc:
                cur_box[0]['all_score'] = all_score.ravel().tolist()
    return tubelets_proto

def rcnn_sampling_dets_scoring(vid_proto, track_proto, det_proto,
        net, class_idx, rcnn_model, overlap_thres=0.7,
        save_feat=False, save_all_sc=False):
    svm_model = svm_from_rcnn_model(rcnn_model)
    tubelets_proto = tubelets_proto_from_tracks_proto(track_proto['tracks'], class_idx)
    logging.info("Scoring {} for {}...".format(vid_proto['video'],
                 imagenet_vdet_classes[class_idx]))
    for frame in vid_proto['frames']:
        frame_id = frame['frame']
        img = imread(frame_path_at(vid_proto, frame_id))
        boxes = [tubelet_box_at_frame(tubelet, frame_id) \
                 for tubelet in tubelets_proto]
        valid_boxes = np.asarray([box for box in boxes if box is not None])
        valid_index = [i for i, box in enumerate(boxes) if box is not None]
        logging.info("frame {}: {} boxes".format(frame_id, len(valid_index)))
        if len(valid_index) == 0:
            continue
        # compute rcnn scores
        features = googlenet_features(img, valid_boxes, net, 'pool5')
        scores = svm_scores(features, svm_model)
        if scores.shape[1] == 200:
            cls_scores = scores[:, index_vdet_to_det[class_idx] - 1]
        else:
            raise

        # find all detection proposal boxes in current frame
        dets = [det for det in det_proto['detections'] if det['frame']==frame_id]
        det_boxes = np.asarray(map(lambda x:x['bbox'], dets))
        det_scores = np.asarray(map(lambda x:det_score(x, class_idx), dets))

        for score, tubelet_id, feat, all_score in \
                zip(cls_scores, valid_index, features, scores):
            cur_box = [box for box in tubelets_proto[tubelet_id]['boxes'] \
                if box['frame'] == frame_id]
            assert len(cur_box) == 1
            # calculate overlaps with all det_boxes in current frame
            if len(det_boxes) > 0:
                overlaps = iou([cur_box[0]['bbox']], det_boxes)
                conf_idx = (overlaps > overlap_thres).ravel()
            else:
                conf_idx = [False]
            if np.any(conf_idx):
                conf_boxes = det_boxes[conf_idx]
                conf_scores = det_scores[conf_idx]
                max_idx = np.argmax(conf_scores)
                max_score = conf_scores[max_idx]
                max_box = conf_boxes[max_idx].tolist()
            else:
                max_score = -np.inf
            if max_score > score:
                cur_box[0]['det_score'] = max_score
                cur_box[0]['bbox'] = max_box
                max_feat = googlenet_features(img, [max_box], net, 'pool5')
                max_all_scores = svm_scores(max_feat, svm_model)
                if save_feat:
                    cur_box[0]['feat'] = max_feat.ravel().tolist()
                if save_all_sc:
                    cur_box[0]['all_score'] = max_all_scores.ravel().tolist()
            else:
                cur_box[0]['det_score'] = score
                if save_feat:
                    cur_box[0]['feat'] = feat.ravel().tolist()
                if save_all_sc:
                    cur_box[0]['all_score'] = all_score.ravel().tolist()
    return tubelets_proto


def scoring_tracks(vid_proto, track_proto, annot_proto,
        sc_method, net, class_idx):
    assert vid_proto['video'] == track_proto['video']
    score_proto = {}
    score_proto['video'] = vid_proto['video']
    score_proto['method'] = sc_method.__name__
    tubelets_proto = sc_method(vid_proto, track_proto, net, class_idx)
    if annot_proto is not None:
        tubelets_proto = tubelets_overlap(tubelets_proto, annot_proto, class_idx)
    score_proto['tubelets'] = tubelets_proto
    return score_proto


def classify_tracks(video_proto, track_proto, cls_method, net, class_idx):
    assert video_proto['video'] == track_proto['video']
    cls_track = {}
    cls_track['video'] = video_proto['video']
    cls_track['method'] = cls_method.__name__
    cls_track['tracks'] = cls_method(video_proto, track_proto, net, class_idx)
    return cls_track

