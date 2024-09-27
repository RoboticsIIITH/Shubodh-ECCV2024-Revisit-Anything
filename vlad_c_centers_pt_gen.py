import numpy as np
import matplotlib.pyplot as plt
import h5py
from tqdm import tqdm


import einops as ein
import argparse
from place_rec_global_config import datasets, experiments, workdir_data

import utm
from glob import glob
from collections import defaultdict
import os
from os.path import join
from natsort import natsorted
import cv2
from typing import Literal, List
import torch
from tkinter import *
from utilities import VLAD
from sklearn.decomposition import PCA
import pickle
import faiss
import json
from importlib import reload
import psutil
import sys
import random


def print_memory_usage(message):
	process = psutil.Process()
	memory_info = process.memory_info()
	print(f"{message}: {memory_info.rss / (1024 * 1024):.2f} MB")


if __name__=="__main__":
	# See README_for_global_full.md for more details on how to run this script and what it is about. (TXDX-Later)
	# Usage: python place_rec_global_any_dataset.py --dataset baidu --experiment exp1_global_AnyLoc_VLAD_and_local_FI_LFM_and_crit_num_matches

	parser = argparse.ArgumentParser(description='Global Place Recognition on Any Dataset. See place_rec_global_config.py to see how to give arguments.')
	parser.add_argument('--dataset', required=True, help='Dataset name') # baidu, pitts etc
	parser.add_argument('--vocab-vlad',required=True, choices=['domain', 'map'], help='Vocabulary choice for VLAD. Options: map, domain.')
	args = parser.parse_args()

	print(f"Vocabulary choice for VLAD (domain/map) is {args.vocab_vlad}")

	# Load dataset and experiment configurations
	dataset_config = datasets.get(args.dataset, {})
	if not dataset_config:
		raise ValueError(f"Dataset '{args.dataset}' not found in configuration.")

	print(dataset_config)
	domain = dataset_config['domain_vlad_cluster'] if args.vocab_vlad == 'domain' else dataset_config['map_vlad_cluster']

	# cfg = {'rmin':0, 'desired_width':640, 'desired_height':480} # Note for later: Not using this cfg anywhere in local code currently. Should incorporate as part of local matching later.
	cfg = dataset_config['cfg']

	# if args.dataset == "pitts" or args.dataset.startswith("msls") or args.dataset == "tokyo247" or args.dataset == "torWIC":
	workdir = f'{workdir_data}/{args.dataset}/out'
	os.makedirs(workdir, exist_ok=True)
	# else: 
	#     workdir = f'/ssd_scratch/saishubodh/segments_data/{args.dataset}/out'
	#     os.makedirs(workdir, exist_ok=True)
	#     workdir_data = '/ssd_scratch/saishubodh/segments_data'
	save_path_results = f"{workdir}/results/"



	dino_r_path = f"{workdir}/{dataset_config['dino_h5_filename_r']}"
	f = h5py.File(dino_r_path, "r")
	keys = list(f.keys())
	db_desc=[]
	# imFts=torch.empty((0,49152))
	# imfts_batch = torch.empty((0,49152))
	# idx = np.empty((cfg['desired_height'],cfg['desired_width'],2)).astype('int32')
	# for i in range(cfg['desired_height']):
	#         for j in range(cfg['desired_width']):
	#                 idx[i,j] = np.array([np.clip(i//14,0,cfg['desired_height']//14-1) ,np.clip(j//14,0,cfg['desired_width']//14-1)])
	# i=0

	# # Randomly sample 25% of the keys
	# random.seed(42) 
	# sampled_keys = random.sample(keys, k=len(keys)//4)

	# If you have less memory, you can play with the sample_threshold and sample_threshold value to sample a smaller subset of the dataset
	sample_threshold = 2000 # only apply any sampling if the number of images is greater than this threshold
	if len(keys) > sample_threshold:
		print(f"Applying sampling for large dataset: {args.dataset}")
		# Apply sampling for large datasets
		sample_percentage = 0.3# 30% sampling
		random.seed(42)
		sampled_keys = random.sample(keys, k=int(len(keys) * sample_percentage))  
		keys_to_process = sampled_keys
	else:
		# Process all keys for smaller datasets
		print(f"Processing all images for dataset: {args.dataset}")
		keys_to_process = keys



	# for i in tqdm(range(len(keys))):
	for i in tqdm(range(len(keys_to_process))):


		# 2. Subsampling every 2nd pixel in both height and width, factor of 4
		key = keys_to_process[i]
		original_data = f[key]['ift_dino'][()]

		if len(keys) > sample_threshold:
			# Apply pixel subsampling for large datasets
			subsampled_data = original_data[:, :, ::2, ::2]
		else:
			# Use full resolution for smaller datasets
			subsampled_data = original_data


		dino_desc = torch.from_numpy(subsampled_data.reshape(1, 1536, -1)).to('cuda')
		dino_desc_norm = torch.nn.functional.normalize(dino_desc, dim=1)
		db_desc.append(dino_desc_norm.permute(0, 2, 1).cpu())

		# 1. original way
		# mask_list=[]
		# # dino_desc = torch.from_numpy(f[keys[i]]['ift_dino'][()]).to('cuda')
		# # dino_desc = torch.nn.functional.interpolate(dino_desc, [cfg['desired_height'],cfg['desired_width']], mode="bilinear", align_corners=True)
		# # print(f"Original DINO descriptor shape for image {i}: {dino_desc.shape}")
		# # dino_desc = torch.from_numpy(np.reshape(f[keys[i]]['ift_dino'][()],(1,1536,f[keys[i]]['ift_dino'][()].shape[2]*f[keys[i]]['ift_dino'][()].shape[3]))).to('cuda')
		# dino_desc = torch.from_numpy(np.reshape(f[keys[i]]['ift_dino'][()],(1,1536,-1))).to('cuda')
		# print(f"now DINO descriptor shape for image {i}: {dino_desc.shape}")
		# # dino_desc=torch.reshape(dino_desc,(1,1536,500*500)).to('cuda')
		# dino_desc_norm = torch.nn.functional.normalize(dino_desc, dim=1)
		# db_desc.append(dino_desc_norm.permute(0,2,1).cpu())




	db_desc=torch.cat(db_desc,dim=0)

	device = torch.device("cuda")
	# Dino_v2 properties (parameters)
	desc_layer: int = 31
	desc_facet: Literal["query", "key", "value", "token"] = "value"
	num_c: int = 32
	# Domain for use case (deployment environment)
	# domain: Literal["aerial", "indoor", "urban"] = "urban"
	# domain =dataset_config['map_vlad_cluster'] #"habitat" #"eiffel"
	print("DOMAIN is ", domain, "PLEASE CHECK")
	cache_dir = './cache'
	ext_specifier = f"dinov2_vitg14/l{desc_layer}_{desc_facet}_c{num_c}"
	c_centers_file = os.path.join(cache_dir, "vocabulary", ext_specifier,
				domain, "c_centers.pt")
	print(f"writing to {c_centers_file}: PLEASE CHECK if path is correct")
	vlad = VLAD(num_c, desc_dim=None, 
		cache_dir=os.path.dirname(c_centers_file))

	print_memory_usage("Before VLAD fitting")


	vlad.fit(ein.rearrange(db_desc, "n k d -> (n k) d"))
	print("VLAD final output : cluster centers shape:", vlad.c_centers.shape)

	print_memory_usage("After VLAD fitting")
