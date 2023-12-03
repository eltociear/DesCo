# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import datetime
import logging
import time
import os
import re

import torch
from tqdm import tqdm
from collections import defaultdict
import torch.distributed as dist

from maskrcnn_benchmark.data.datasets.evaluation import evaluate, im_detect_bbox_aug
from ..utils.comm import is_main_process
from ..utils.comm import all_gather
from ..utils.comm import synchronize
from .tsv_saver import TSVResultWriter
import pdb
from maskrcnn_benchmark.data.datasets.evaluation.flickr.flickr_eval import FlickrEvaluator

from maskrcnn_benchmark.data.datasets.refexp import RefExpEvaluator
from maskrcnn_benchmark.structures.bounding_box import BoxList
import matplotlib.pyplot as plt
import matplotlib.pylab as pylab
from maskrcnn_benchmark.data.datasets.tsv import load_from_yaml_file
from sentence_transformers import SentenceTransformer
from numpy.random import RandomState
import fastcluster
import collections
import scipy
import numpy as np
import scipy.cluster
import sklearn
import base64
import cv2, json
from maskrcnn_benchmark.structures.boxlist_ops import cat_boxlist
from maskrcnn_benchmark.data.datasets.od_to_grounding import clean_name
from maskrcnn_benchmark.data.datasets._od_to_description import DescriptionConverter

from copy import deepcopy
from pprint import pprint
import wandb
def imshow(img, file_name = "tmp.jpg"):
    plt.imshow(img[:, :, [2, 1, 0]])
    plt.axis("off")
    #plt.figtext(0.5, 0.09, "test", wrap=True, horizontalalignment='center', fontsize=20)
    plt.savefig(file_name)
def load(url_or_file_name):
    try:
        response = requests.get(url_or_file_name)
    except:
        response = None
    if response is None:
        pil_image = Image.open(url_or_file_name).convert("RGB")
    else:
        pil_image = Image.open(BytesIO(response.content)).convert("RGB")
    # convert to BGR format
    image = np.array(pil_image)[:, :, [2, 1, 0]]
    return image

def inference_default(
    model,
    data_loader,
    dataset_name,
    iou_types=("bbox",),
    box_only=False,
    device="cuda",
    expected_results=(),
    expected_results_sigma_tol=4,
    output_folder=None,
    cfg=None,
):
    # convert to a torch.device for efficiency
    device = torch.device(device)
    num_devices = torch.distributed.get_world_size() if torch.distributed.is_initialized() else 1
    logger = logging.getLogger("maskrcnn_benchmark.inference")
    dataset = data_loader.dataset
    logger.info("Start evaluation on {} dataset({} images).".format(dataset_name, len(dataset)))
    start_time = time.time()

    model.eval()
    results_dict = {}
    cpu_device = torch.device("cpu")
    for i, batch in enumerate(tqdm(data_loader)):
        images, targets, image_ids, *_ = batch
        with torch.no_grad():
            if cfg.TEST.USE_MULTISCALE:
                output = im_detect_bbox_aug(model, images, device)
            else:
                output = model(images.to(device))
            output = [o.to(cpu_device) for o in output]
        results_dict.update({img_id: result for img_id, result in zip(image_ids, output)})
    predictions = results_dict
    # wait for all processes to complete before measuring the time
    synchronize()
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=total_time))
    logger.info(
        "Total inference time: {} ({} s / img per device, on {} devices)".format(
            total_time_str, total_time * num_devices / len(dataset), num_devices
        )
    )

    predictions = _accumulate_predictions_from_multiple_gpus(predictions)
    if not is_main_process():
        return None

    if output_folder:
        torch.save(predictions, os.path.join(output_folder, "predictions.pth"))

    extra_args = dict(
        box_only=box_only,
        iou_types=iou_types,
        expected_results=expected_results,
        expected_results_sigma_tol=expected_results_sigma_tol,
    )
    return evaluate(dataset=dataset, predictions=predictions, output_folder=output_folder, **extra_args)


def clean_name(name):
    name = re.sub(r"\(.*\)", "", name)
    name = re.sub(r"_", " ", name)
    name = re.sub(r"  ", " ", name)
    return name


def create_one_hot_dict(labels, no_minus_one_for_one_hot=False):
    positive_map_token_to_label = defaultdict(int)
    positive_map_label_to_token = defaultdict(int)

    for i in range(len(labels)):
        positive_map_token_to_label[i] = labels[i]
        positive_map_label_to_token[labels[i]] = i

    if no_minus_one_for_one_hot:
        positive_map_token_to_label = defaultdict(int)
        positive_map_label_to_token = defaultdict(int)

        for i in range(len(labels)):
            positive_map_token_to_label[i + 1] = labels[i]
            positive_map_label_to_token[labels[i]] = i + 1

    return positive_map_token_to_label, positive_map_label_to_token


def create_positive_dict(tokenized, tokens_positive, labels):
    """construct a dictionary such that positive_map[i] = j, iff token i is mapped to j label"""
    positive_map = defaultdict(int)

    # Additionally, have positive_map_label_to_tokens
    positive_map_label_to_token = defaultdict(list)

    for j, tok_list in enumerate(tokens_positive):
        for (beg, end) in tok_list:
            beg_pos = tokenized.char_to_token(beg)
            end_pos = tokenized.char_to_token(end - 1)
            if beg_pos is None:
                try:
                    beg_pos = tokenized.char_to_token(beg + 1)
                    if beg_pos is None:
                        beg_pos = tokenized.char_to_token(beg + 2)
                except:
                    beg_pos = None
            if end_pos is None:
                try:
                    end_pos = tokenized.char_to_token(end - 2)
                    if end_pos is None:
                        end_pos = tokenized.char_to_token(end - 3)
                except:
                    end_pos = None
            if beg_pos is None or end_pos is None:
                continue

            assert beg_pos is not None and end_pos is not None
            for i in range(beg_pos, end_pos + 1):
                positive_map[i] = labels[j]  # because the labels starts from 1
                positive_map_label_to_token[labels[j]].append(i)
            # positive_map[j, beg_pos : end_pos + 1].fill_(1)
    return positive_map, positive_map_label_to_token  # / (positive_map.sum(-1)[:, None] + 1e-6)


def chunks(lst, n):
    """Yield successive n-sized chunks from lst."""
    all_ = []
    for i in range(0, len(lst), n):
        data_index = lst[i : i + n]
        all_.append(data_index)
    counter = 0
    for i in all_:
        counter += len(i)
    assert counter == len(lst)

    return all_


sbert_model = None
def _get_sbert_model():
    global sbert_model
    if not sbert_model:
        sbert_model = SentenceTransformer('paraphrase-MiniLM-L6-v2')
    return sbert_model

def semantic_deduplicate_captions(captions,
                                  label_list, 
                                  keep_p=.8,
                                  must_keep_idxs=None,
                                  seed=1, verbose=False,
                                  return_features=False,
                                  force_exact=False):
    '''
    keep_p can be a proportion to keep, e.g., .5, or it can be an int representing the number to keep, like 10.
    '''
    original_captions = deepcopy(captions)
    captions = ["This is " + c for c in captions]
    prng = RandomState(seed)

    must_keep_idxs = set(must_keep_idxs) if must_keep_idxs is not None else set()

    sbert =_get_sbert_model()
    features = sbert.encode([c for c in captions], show_progress_bar=verbose)
    pdists = sklearn.metrics.pairwise_distances(features, metric='cosine')
    # numerical issues...
    np.fill_diagonal(pdists, 0.0)
    pdists = (pdists + pdists.transpose()) / 2
    pdists = scipy.spatial.distance.squareform(pdists)
    res = fastcluster.linkage(pdists, method='average', preserve_input=False)
    del pdists
    if keep_p < 1:
        n_keep_from_cluster = int(np.round(keep_p*(len(captions))))
    else:
        n_keep_from_cluster = min(keep_p, len(captions))
    print('going to keep {} out of {} captions'.format(n_keep_from_cluster, len(captions)))
    clusters = scipy.cluster.hierarchy.fcluster(res, n_keep_from_cluster, criterion='maxclust')

    cluster2idxs = collections.defaultdict(list)
    for idx, cluster in enumerate(clusters):
        cluster2idxs[cluster].append(idx)

    # algo:
    # 1. go through clusters with must includes, add must keeps.
    # 2. for each cluster without a must keep, add it to candidate list, shuffle candidate list
    # 3. loop over each candidate in the candidate list until the return set is the correct size.

    chunked_labels = []
    chunked_label_list = []
    for c, idxs in cluster2idxs.items():
        chunked_labels.append([original_captions[i] for i in idxs])
        chunked_label_list.append([label_list[i] for i in idxs])    
    print("size of each prompt:", [len(i) for i in chunked_labels])
    return chunked_labels, chunked_label_list
    

def create_queries_and_maps_from_dataset(dataset, cfg):
    categories = dataset.categories()
    # one_hot = dataset.one_hot

    labels = []
    label_list = []
    keys = list(categories.keys())
    keys.sort()
    for i in keys:
        labels.append(i)
        label_list.append(categories[i])

    if cfg.TEST.CHUNKED_EVALUATION != -1:
        if cfg.TEST.CHUNK_METHOD == "similar":
            label_list, labels = semantic_deduplicate_captions(
                label_list, labels, keep_p=len(labels) // cfg.TEST.CHUNKED_EVALUATION,)
        else:
            labels = chunks(labels, cfg.TEST.CHUNKED_EVALUATION)
            label_list = chunks(label_list, cfg.TEST.CHUNKED_EVALUATION)
    else:
        labels = [labels]
        label_list = [label_list]

    all_queries = []
    all_positive_map_label_to_token = []

    for i in range(len(labels)):
        labels_i = labels[i]
        label_list_i = label_list[i]
        query_i, positive_map_label_to_token_i = create_queries_and_maps(
            labels_i,
            label_list_i,
            additional_labels=cfg.DATASETS.SUPRESS_QUERY if cfg.DATASETS.USE_SUPRESS_QUERY else None,
            cfg=cfg,
        )

        all_queries.append(query_i)
        all_positive_map_label_to_token.append(positive_map_label_to_token_i)
    print("All queries", all_queries)
    return all_queries, all_positive_map_label_to_token

def create_queries_and_maps(labels, label_list, additional_labels=None, cfg=None):

    # Clean label list
    label_list = [clean_name(i) for i in label_list]
    # Form the query and get the mapping
    tokens_positive = []
    start_i = 0
    end_i = 0
    objects_query = ""

    # sep between tokens, follow training
    separation_tokens = cfg.DATASETS.SEPARATION_TOKENS

    caption_prompt = cfg.DATASETS.CAPTION_PROMPT
    if caption_prompt is not None and isinstance(caption_prompt, str):
        caption_prompt = load_from_yaml_file(caption_prompt)
    use_caption_prompt = cfg.DATASETS.USE_CAPTION_PROMPT and caption_prompt is not None
    for _index, label in enumerate(label_list):
        if use_caption_prompt:
            objects_query += caption_prompt[_index]["prefix"]

        start_i = len(objects_query)

        if use_caption_prompt:
            objects_query += caption_prompt[_index]["name"]
        else:
            objects_query += label

        end_i = len(objects_query)
        tokens_positive.append([(start_i, end_i)])  # Every label has a [(start, end)]

        if use_caption_prompt:
            objects_query += caption_prompt[_index]["suffix"]

        if _index != len(label_list) - 1:
            objects_query += separation_tokens

    if additional_labels is not None:
        objects_query += separation_tokens
        for _index, label in enumerate(additional_labels):
            objects_query += label
            if _index != len(additional_labels) - 1:
                objects_query += separation_tokens

    print(objects_query)

    from transformers import AutoTokenizer

    # tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
    if cfg.MODEL.LANGUAGE_BACKBONE.TOKENIZER_TYPE == "bert-base-uncased":
        tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
        tokenized = tokenizer(objects_query, return_tensors="pt")
    elif cfg.MODEL.LANGUAGE_BACKBONE.TOKENIZER_TYPE == "roberta-base":
        tokenizer = AutoTokenizer.from_pretrained("roberta-base")
        tokenized = tokenizer(objects_query, return_tensors="pt")
    elif cfg.MODEL.LANGUAGE_BACKBONE.TOKENIZER_TYPE == "clip":
        from transformers import CLIPTokenizerFast

        if cfg.MODEL.DYHEAD.FUSE_CONFIG.MLM_LOSS:
            tokenizer = CLIPTokenizerFast.from_pretrained(
                "openai/clip-vit-base-patch32", from_slow=True, mask_token="ðŁĴĳ</w>"
            )
        else:
            tokenizer = CLIPTokenizerFast.from_pretrained("openai/clip-vit-base-patch32", from_slow=True)
        tokenized = tokenizer(
            objects_query, max_length=cfg.MODEL.LANGUAGE_BACKBONE.MAX_QUERY_LEN, truncation=True, return_tensors="pt"
        )
    else:
        tokenizer = None
        raise NotImplementedError

    # Create the mapping between tokenized sentence and the original label
    # if one_hot:
    #     positive_map_token_to_label, positive_map_label_to_token = create_one_hot_dict(labels, no_minus_one_for_one_hot=cfg.DATASETS.NO_MINUS_ONE_FOR_ONE_HOT)
    # else:
    positive_map_token_to_label, positive_map_label_to_token = create_positive_dict(
        tokenized, tokens_positive, labels=labels
    )  # from token position to original label
    return objects_query, positive_map_label_to_token


def create_positive_map_label_to_token_from_positive_map(positive_map, plus=0):
    positive_map_label_to_token = {}
    for i in range(len(positive_map)):
        positive_map_label_to_token[i + plus] = torch.nonzero(positive_map[i], as_tuple=True)[0].tolist()
    return positive_map_label_to_token


def _accumulate_predictions_from_multiple_gpus(predictions_per_gpu):
    all_predictions = all_gather(predictions_per_gpu)
    if not is_main_process():
        return
    # merge the list of dicts
    predictions = {}
    for p in all_predictions:
        predictions.update(p)
    # convert a dict where the key is the index in a list
    image_ids = list(sorted(predictions.keys()))
    if len(image_ids) != image_ids[-1] + 1:
        logger = logging.getLogger("maskrcnn_benchmark.inference")
        logger.warning(
            "Number of images that were gathered from multiple processes is not "
            "a contiguous set. Some images might be missing from the evaluation"
        )

    # convert to a list
    predictions = [predictions[i] for i in image_ids]
    return predictions


def resize_box(output, targets):
    if isinstance(targets[0], dict):
        orig_target_sizes = targets[0]["orig_size"].unsqueeze(0)
    else:
        orig_target_sizes = torch.stack([targets[0].extra_fields["orig_size"] for _ in range(1)], dim=0)
    img_h, img_w = orig_target_sizes.unbind(1)
    return output.resize((img_w, img_h))


def flickr_post_process(output, targets, positive_map_label_to_token, plus):
    raw_boxes = deepcopy(output.bbox)
    output = resize_box(output, targets)
    scores, indices = torch.topk(output.extra_fields["scores"], k=len(output.extra_fields["scores"]), sorted=True)
    boxes = output.bbox.tolist()
    boxes = [boxes[i] for i in indices]
    labels = [output.extra_fields["labels"][i] for i in indices]
    output_boxes = [[] for i in range(len(positive_map_label_to_token))]
    output_scores = [[] for i in range(len(positive_map_label_to_token))]
    for i in range(len(boxes)):
        output_boxes[labels[i] - plus].append(boxes[i])
        output_scores[labels[i] - plus].append(scores[i])
    for i in output_boxes:
        i.append([0.0, 0.0, 0.0, 0.0])
    image_ids = [t.extra_fields["original_img_id"] for t in targets]
    sentence_ids = [t.extra_fields["sentence_id"] for t in targets]

    return {"image_id": image_ids[0], "sentence_id": sentence_ids[0], "boxes": output_boxes, "scores": output_scores, "raw_boxes": raw_boxes}

def post_process(dataset_name, output, targets, positive_map_label_to_token, plus, categories = None, captions = None):
    '''
    Transfer the output from the model to appropriate formats for evaluation
    '''
    if "flickr" in dataset_name:
        output = output[0]
        raw_boxes = deepcopy(output.bbox)
        new_output = flickr_post_process(
            output, targets, positive_map_label_to_token, plus  # This is only used in Flickr
        )
        visualization_output = (
            new_output["image_id"], 
            {
                "boxes": new_output["boxes"], 
                "scores": new_output["scores"],
                "raw_boxes": raw_boxes,
                }
            )
    elif "lvis" in dataset_name:
        output = output[0]
        raw_boxes = deepcopy(output.bbox)
        output = resize_box(output, targets)
        scores = output.extra_fields["scores"]
        labels = output.extra_fields["labels"]
        boxes = output.bbox
        new_output = (targets[0]["image_id"].item(), {"scores": scores, "labels": labels, "boxes": boxes, "raw_boxes": raw_boxes, "labels_text": [categories[cat_id.item()] for cat_id in labels]})
        visualization_output = new_output[1]
    elif "refcoco" in dataset_name:
        output = output[0]
        output = resize_box(output, targets)
        scores = output.extra_fields["scores"]
        boxes = output.bbox
        image_id = [t.extra_fields["image_id"] for t in targets][0].item()
        new_output = {image_id: {"scores": scores, "boxes": boxes}}
        visualization_output = {"scores": scores, "boxes": boxes}
    else:
        new_output = output
        visualization_output = output
    return new_output, visualization_output

def process_for_vis(dataset_name, image_ids, visualization_outputs):
    '''
    Transfer the output from the model to appropriate formats for visualization
    '''
    if "lvis" in dataset_name:
        assert(len(image_ids) == 1)
        # merge the visualization_outputs
        visualization_output = {}
        if len(visualization_outputs) > 0:
            for key in visualization_outputs[0].keys():
                if key == "labels_text":
                    _labels_text = [v[key] for v in visualization_outputs]
                    visualization_output[key] = [item for sublist in _labels_text for item in sublist]
                else:
                    visualization_output[key] = torch.cat([v[key] for v in visualization_outputs], dim=0)
        visualization_output = [(image_ids[0], visualization_output)] #
    return visualization_output

def write_to_wandb_log(score, dataset_name, weight_iter, history):
    all_results = defaultdict(dict)
    exclude_keys = ['_step', '_runtime', '_timestamp']
    if history is not None:
        for stat in history:
           all_results[stat['_step']].update({k: v for k, v in stat.items() if k not in exclude_keys})
    if "lvis" in dataset_name.lower():
        mAP_all = float(score[0].split("Average Precision  (AP) @[ IoU=0.50:0.95 | area=   all | maxDets= -1 catIds=all] = ")[-1])
        mAP_rare = float(score[6].split("Average Precision  (AP) @[ IoU=0.50:0.95 | area=   all | maxDets= -1 catIds=  r] = ")[-1])
        mAP_common = float(score[7].split("Average Precision  (AP) @[ IoU=0.50:0.95 | area=   all | maxDets= -1 catIds=  c] = ")[-1])
        mAP_frequent = float(score[8].split("Average Precision  (AP) @[ IoU=0.50:0.95 | area=   all | maxDets= -1 catIds=  f] = ")[-1])
        #wandb.log({f"{dataset_name}_mAP_all": mAP_all, f"{dataset_name}_mAP_rare": mAP_rare, f"{dataset_name}_mAP_common": mAP_common, f"{dataset_name}_mAP_frequent": mAP_frequent},  step = weight_iter)
        all_results[weight_iter].update({f"{dataset_name}_mAP_all": mAP_all, f"{dataset_name}_mAP_rare": mAP_rare, f"{dataset_name}_mAP_common": mAP_common, f"{dataset_name}_mAP_frequent": mAP_frequent})
    elif "flickr" in dataset_name.lower():
        recall_1 = score["Recall@1_all"]
        recall_5 = score["Recall@5_all"]
        recall_10 = score["Recall@10_all"]
        # wandb.log(
        #     {f"{dataset_name}_recall@1": recall_1, f"{dataset_name}_recall@5": recall_5, f"{dataset_name}_recall@10": recall_10}, step = weight_iter
        # )
        all_results[weight_iter].update({f"{dataset_name}_recall@1": recall_1, f"{dataset_name}_recall@5": recall_5, f"{dataset_name}_recall@10": recall_10})
    elif "coco" in dataset_name.lower():
        all_results[weight_iter].update({f"{dataset_name}_mAP": score[0].results['bbox']['AP']})
    
    # sort all results 
    max_key = max(all_results.keys())
    for i in range(max_key + 1):
        if i in all_results:
            wandb.log(all_results[i], step = i)
        else:
            wandb.log({}, step = i)
    # for k in sorted(all_results.keys()):
    #     # need to do consecutive logging
    #     wandb.log(all_results[k], step = k)


def build_flickr_evaluator(cfg):
    evaluator = FlickrEvaluator(
        "DATASET/flickr30k/flickr30k/",  # Hard written!!
        subset="test" if "test" in cfg.DATASETS.TEST[0] else "val",
        merge_boxes=cfg.DATASETS.FLICKR_GT_TYPE == "merged",
    )
    return evaluator


def build_refexp_evaluator(dataset):
    from maskrcnn_benchmark.data.datasets.refexp import RefExpDataset

    evaluator = RefExpEvaluator(dataset.coco, ("bbox"))
    return evaluator


def build_lvis_evaluator(ann_file, topk, fixed_ap=True):
    from maskrcnn_benchmark.data.datasets.evaluation.lvis.lvis import LVIS
    from maskrcnn_benchmark.data.datasets.evaluation.lvis.lvis_eval import LvisEvaluatorFixedAP, LvisEvaluator
    evaluator = LvisEvaluatorFixedAP(LVIS(ann_file), topk = topk, fixed_ap=fixed_ap) # topk
    #evaluator = LvisEvaluator(LVIS(ann_file), iou_types=['segm', 'bbox'])
    return evaluator


def write_lvis_results(results, output_file_name):
    if isinstance(results, dict):
        output_file_name = output_file_name.replace("bbox.csv", "coco_results.pth")
        torch.save(results, output_file_name)
        return

    lines = []
    lines.append("metric, avg ")
    for each_result in results:
        metric_string = " ".join(each_result.split(" ")[:-2])
        number = each_result.split(" ")[-1]
        each_result = metric_string + ", " + number + " "
        lines.append(each_result)

    string_to_write = "\n".join(lines) + "\n"
    with open(output_file_name, "w") as f:
        f.write(string_to_write)
    return


def write_flickr_results(results, output_file_name):
    lines = []
    lines.append("metric, avg ")
    for each_metric, number in results.items():
        each_result = each_metric + ", " + str(number) + " "
        lines.append(each_result)

    string_to_write = "\n".join(lines) + "\n"
    with open(output_file_name, "w") as f:
        f.write(string_to_write)
    return


def write_refexp_results(results, output_file_name):
    lines = []
    lines.append("metric, avg ")
    for each_metric, recall_list in results.items():
        for i, recall in zip(
            [1, 5, 10],
            recall_list,
        ):
            each_result = each_metric + ": " + f"Recall@{i} = " + str(recall) + " "
            lines.append(each_result)

    string_to_write = "\n".join(lines) + "\n"
    with open(output_file_name, "w") as f:
        f.write(string_to_write)
    return


def inference(
    model,
    data_loader,
    dataset_name,
    iou_types=("bbox",),
    box_only=False,
    device="cuda",
    expected_results=(),
    expected_results_sigma_tol=4,
    output_folder=None,
    cfg=None,
    verbose=True,
    weight_iter = None,
    wandb_run=None,
    history=None
):
    # convert to a torch.device for efficiency
    try:
        device = torch.device(device)
    except:
        device = device
    num_devices = torch.distributed.get_world_size() if torch.distributed.is_initialized() else 1
    logger = logging.getLogger("maskrcnn_benchmark.inference")
    dataset = data_loader.dataset
    if verbose:
        logger.info("Start evaluation on {} dataset({} images).".format(dataset_name, len(dataset)))
    start_time = time.time()

    task = cfg.TEST.EVAL_TASK

    if not task:
        return inference_default(
            model,
            data_loader,
            dataset_name,
            iou_types,
            box_only,
            device,
            expected_results,
            expected_results_sigma_tol,
            output_folder,
            cfg,
        )

    if task == "detection":
        if "description" in cfg.DATASETS.OD_TO_GROUNDING_VERSION:
            try:
                descriptions = dataset.lvis.dataset["categories"]
            except:
                descriptions = dataset.coco.dataset["categories"]
            od_grounding_converter = DescriptionConverter(
                cfg.DATASETS.DESCRIPTION_FILE,
                cfg.DATASETS.OD_TO_GROUNDING_VERSION,
                descriptions,
                dataset.categories()) # the last parameters is a bit ad-hoc
            all_queries, all_positive_map_label_to_token = od_grounding_converter.inference_od_to_grounding(dataset, cfg)
        else:
            all_queries, all_positive_map_label_to_token = create_queries_and_maps_from_dataset(dataset, cfg)
    elif task == "grounding":
        all_queries = [None]
        all_positive_map_label_to_token = [None]
    else:
        assert 0

    """
    Build Dataset Sepecific Evaluator
    """
    if "flickr" in cfg.DATASETS.TEST[0]:
        evaluator = build_flickr_evaluator(cfg)
    elif "lvis" in cfg.DATASETS.TEST[0]:
        evaluator = build_lvis_evaluator(dataset.ann_file, topk=cfg.DATASETS.LVIS_TOPK, fixed_ap=not cfg.DATASETS.LVIS_USE_NORMAL_AP)
    elif "refcoco" in cfg.DATASETS.TEST[0]:
        evaluator = build_refexp_evaluator(dataset)
    else:
        evaluator = None

    model.eval()
    results_dict = {}
    cpu_device = torch.device("cpu")
    if verbose:
        _iterator = tqdm(data_loader)
    else:
        _iterator = data_loader
    # save the visualization results
    max_visualize_num = 1000
    gold_data_tsv = TSVResultWriter(
        max_visualize_num=max_visualize_num,
        file_name=os.path.join(output_folder, "gold_{}/test.tsv").format(torch.distributed.get_rank() if torch.distributed.is_initialized() else 0,
        write_freq=10)
    )
    prediction_data_tsv = TSVResultWriter(
        max_visualize_num=max_visualize_num,
        file_name=os.path.join(output_folder, "prediction_{}/test.tsv").format(torch.distributed.get_rank() if torch.distributed.is_initialized() else 0),
        write_freq=10)



    try:
        categories = dataset.categories()
        raw_categories = dataset.lvis.dataset["categories"]
        raw_categories = {c["id"]: c for c in raw_categories}
    except:
        categories = None
        raw_categories = None
    for i, batch in enumerate(_iterator):
        if i == cfg.TEST.SUBSET:
            break
        images, targets, image_ids, *_ = batch
        gold_data_tsv.update_gold_od_data(images, targets, raw_categories)

        all_output = []
        mdetr_style_output = []
        visualization_outputs = []

        all_labels = set()
        assert len(targets) == 1
        for l in targets[0]['labels']: #assert len(targets) == 1
            all_labels.add(int(l))

        negative_index = 4 #0:pos, 1-5: neg
        for cur_label in all_labels:
            with torch.no_grad():
                all_queries, all_positive_map_label_to_token = od_grounding_converter.inference_od_to_grounding(dataset, cfg, negative_label=cur_label, negative_index=negative_index)
                if cfg.TEST.USE_MULTISCALE:
                    query_time = len(all_queries)
                    for query_i in range(query_time):
                        if task == "detection":
                            captions = [all_queries[query_i] for ii in range(len(targets))]
                            positive_map_label_to_token = all_positive_map_label_to_token[query_i]
                        else:
                            captions = None
                            positive_map_label_to_token = None

                    output = im_detect_bbox_aug(model, images, device, captions, positive_map_label_to_token)
                    output = [o.to(cpu_device) for o in output]
                    all_output.append(output)
                else:
                    images = images.to(device)
                    query_time = len(all_queries)

                    output_for_one_image = []
                    for query_i in range(query_time):
                        if not isinstance(targets[0], dict):  # For LVIS dataset and datasets directly copied from MDETR
                            targets = [target.to(device) for target in targets]
                        """
                        different datasets seem to have different data format... For LVIS dataset, the target is a dictionary, while for modulatedDataset such as COCO/Flickr, the target is a BoxList
                        """

                        if task == "detection":
                            captions = [all_queries[query_i] for ii in range(len(targets))]
                            positive_map_label_to_token = all_positive_map_label_to_token[query_i]
                            if cfg.MODEL.DYHEAD.FUSE_CONFIG.SPAN_VERSION is not None:
                                positive_map_label_to_token, span_map, spans = positive_map_label_to_token
                                spans = [spans] # Let's just use one image per batch
                            else:
                                span_map = None
                                spans = None
                        elif task == "grounding":
                            captions = [t.get_field("caption") for t in targets]
                            positive_map_eval = [
                                t.get_field("positive_map_eval")
                                if t.has_field("positive_map_eval")
                                else t.get_field("positive_map")
                                for t in targets
                            ]
                            if cfg.MODEL.RPN_ARCHITECTURE == "VLDYHEAD":
                                plus = 1
                            else:
                                plus = 0
                            assert len(positive_map_eval) == 1  # Let's just use one image per batch
                            positive_map_eval = positive_map_eval[0]
                            positive_map_label_to_token = create_positive_map_label_to_token_from_positive_map(
                                positive_map_eval, plus=plus
                            )
                            span_map = None
                            spans = None
                        output = model(images, captions=captions, positive_map=positive_map_label_to_token, spans = spans, span_map=span_map)
                        if cfg.TEST.CHUNK_INFERENCE_VERSION == "v2":
                            assert(len(output) == 1)
                            output_for_one_image.append(output[0])
                        else:
                            output = [o.to(cpu_device) for o in output]
                            if cfg.MODEL.RPN_ARCHITECTURE == "VLDYHEAD":
                                plus = 1
                            else:
                                plus = 0
                            output, visualization_output = post_process(
                                cfg.DATASETS.TEST[0],
                                output, targets, positive_map_label_to_token, plus=plus, categories=categories, captions=captions)
                            if evaluator is not None:
                                mdetr_style_output.append(output)
                            else:
                                all_output.append(output)
                            visualization_outputs.append(visualization_output)
                        
                        if cfg.TEST.CHUNK_INFERENCE_VERSION == "v2":
                            # merge boxes
                            output = cat_boxlist(output_for_one_image)
                            output = model.rpn.box_selector_test.select_over_all_levels([output])
                            output = [o.to(cpu_device) for o in output]
                            if cfg.MODEL.RPN_ARCHITECTURE == "VLDYHEAD":
                                plus = 1
                            else:
                                plus = 0
                            output, visualization_output = post_process(
                                output, targets, positive_map_label_to_token, plus=plus, categories=categories,)
                            
                            if evaluator is not None:
                                mdetr_style_output.append(output)
                            else:
                                all_output.append(output)
                            visualization_outputs.append(visualization_output)



        prediction_data_tsv.update(images, process_for_vis(cfg.DATASETS.TEST[0], image_ids, visualization_outputs)) # write the prediction data to tsv file
        
        if evaluator is not None:
            try:
                evaluator.update(mdetr_style_output)
            except:
                evaluator.update(mdetr_style_output[0])
        else:
            output = [[row[_i] for row in all_output] for _i in range(len(all_output[0]))]
            for index, i in enumerate(output):
                output[index] = i[0].concate_box_list(i)

            results_dict.update({img_id: result for img_id, result in zip(image_ids, output)})

    if evaluator is not None:
        evaluator.synchronize_between_processes()
        try:
            evaluator.accumulate()
        except:
            print("Evaluator has no accumulation, skipped...")
        
        try:
            score, results_processed = evaluator.summarize()
            pprint(results_processed)
        except:
            score = evaluator.summarize()
            results_processed = None
        
        if is_main_process():
            if wandb_run is not None:
                #
                dataset_name = cfg.DATASETS.TEST[0]
                write_to_wandb_log(score, dataset_name, weight_iter, history)
                
                with open("{}/detailed.json".format(output_folder), "w") as f:
                    json.dump(results_processed, f)
                wandb_run.save("{}/detailed.json".format(output_folder))
        
        pprint(score)
        import maskrcnn_benchmark.utils.mdetr_dist as dist
        if is_main_process():
            if "flickr" in cfg.DATASETS.TEST[0]:
                write_flickr_results(score, output_file_name=os.path.join(output_folder, "bbox.csv"))
            elif "lvis" in cfg.DATASETS.TEST[0]:
                write_lvis_results(score, output_file_name=os.path.join(output_folder, "bbox.csv"))
            elif "refcoco" in cfg.DATASETS.TEST[0] and output_folder is not None:
                write_refexp_results(score, output_file_name=os.path.join(output_folder, "Recall_results.csv"))
        try:
            torch.distributed.barrier()
        except:
            print("Default process group is not initialized")
        return

    if evaluator is not None:
        predictions = mdetr_style_output
    else:
        predictions = results_dict
    # wait for all processes to complete before measuring the time
    synchronize()
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=total_time))
    logger.info(
        "Total inference time: {} ({} s / img per device, on {} devices)".format(
            total_time_str, total_time * num_devices / len(dataset), num_devices
        )
    )

    predictions = _accumulate_predictions_from_multiple_gpus(predictions)
    print("Accumulated results")
    if not is_main_process():
        return None

    if output_folder:
        torch.save(predictions, os.path.join(output_folder, "predictions.pth"))

    extra_args = dict(
        box_only=box_only,
        iou_types=iou_types,
        expected_results=expected_results,
        expected_results_sigma_tol=expected_results_sigma_tol,
    )
    results = evaluate(dataset=dataset, predictions=predictions, output_folder=output_folder, **extra_args)
    
    if is_main_process():
        if wandb_run is not None:
            dataset_name = cfg.DATASETS.TEST[0]
            write_to_wandb_log(results, dataset_name, weight_iter, history)
            
            # with open("{}/detailed.json".format(output_folder), "w") as f:
            #     json.dump(results, f)
            # wandb_run.save("{}/detailed.json".format(output_folder))
    return results