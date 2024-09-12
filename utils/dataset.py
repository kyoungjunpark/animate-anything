import os
import decord
import numpy as np
import random
import json
import torchvision
import torchvision.transforms as T
import torch

from glob import glob
from PIL import Image
from itertools import islice
from pathlib import Path
from .bucketing import sensible_buckets
from .common import get_moved_area_mask, calculate_motion_score

decord.bridge.set_bridge('torch')

from torch.utils.data import Dataset
from einops import rearrange, repeat


# Inspired by the VideoMAE repository.
def normalize_input(
        item,
        mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225],
        use_simple_norm=True
):
    if item.dtype == torch.uint8 and not use_simple_norm:
        item = rearrange(item, 'f c h w -> f h w c')

        item = item.float() / 255.0
        mean = torch.tensor(mean)
        std = torch.tensor(std)

        out = rearrange((item - mean) / std, 'f h w c -> f c h w')

        return out
    else:
        # Normalize between -1 & 1
        item = rearrange(item, 'f c h w -> f h w c')
        return rearrange(item / 127.5 - 1.0, 'f h w c -> f c h w')


def get_prompt_ids(prompt, tokenizer):
    if tokenizer is None:
        prompt_ids = torch.tensor([0])
    else:
        prompt_ids = tokenizer(
            prompt,
            truncation=True,
            padding="max_length",
            max_length=tokenizer.model_max_length,
            return_tensors="pt",
        ).input_ids[0]
    return prompt_ids


def read_caption_file(caption_file):
    with open(caption_file, 'r', encoding="utf8") as t:
        return t.read()


def get_text_prompt(
        text_prompt: str = '',
        fallback_prompt: str = '',
        file_path: str = '',
        ext_types=['.mp4'],
        use_caption=False
):
    try:
        if use_caption:
            if len(text_prompt) > 1: return text_prompt
            caption_file = ''
            # Use caption on per-video basis (One caption PER video)
            for ext in ext_types:
                maybe_file = file_path.replace(ext, '.txt')
                if maybe_file.endswith(ext_types): continue
                if os.path.exists(maybe_file):
                    caption_file = maybe_file
                    break

            if os.path.exists(caption_file):
                return read_caption_file(caption_file)

            # Return fallback prompt if no conditions are met.
            return fallback_prompt

        return text_prompt
    except:
        print(f"Couldn't read prompt caption for {file_path}. Using fallback.")
        return fallback_prompt


def get_frame_batch(max_frames, sample_fps, vr, transform):
    native_fps = vr.get_avg_fps()
    max_range = len(vr)
    frame_step = max(1, round(native_fps / sample_fps))
    frame_range = range(0, max_range, frame_step)
    if len(frame_range) < max_frames:
        frame_range = np.linspace(0, max_range - 1, max_frames).astype(int)
    # start = random.randint(0, len(frame_range) - max_frames)
    start = len(frame_range) - max_frames
    frame_range_indices = list(frame_range)[start:start + max_frames]
    frames = vr.get_batch(frame_range_indices)
    video = rearrange(frames, "f h w c -> f c h w")
    video = transform(video)
    return video


def get_frame_signal_batch(signal_path, max_frames, sample_fps, vr, transform):
    native_fps = vr.get_avg_fps()
    max_range = len(vr)
    frame_step = max(1, round(native_fps / sample_fps))
    frame_range = range(0, max_range, frame_step)
    if len(frame_range) < max_frames:
        frame_range = np.linspace(0, max_range - 1, max_frames).astype(int)
    # start = random.randint(0, len(frame_range) - max_frames)
    start = len(frame_range) - max_frames
    frame_range_indices = list(frame_range)[start:start + max_frames]
    frames = vr.get_batch(frame_range_indices)
    video = rearrange(frames, "f h w c -> f c h w")
    video = transform(video)
    # device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    channels = torch.load(signal_path, map_location="cuda:0", weights_only=True)

    partial_channels = channels[frame_range_indices, :]

    # partial_channels = np.array(0)
    return video, partial_channels


def get_frame_agg_signal_batch(signal_path, max_frames, sample_fps, vr, transform):
    native_fps = vr.get_avg_fps()
    max_range = len(vr)
    frame_step = max(1, round(native_fps / sample_fps))
    frame_range = range(0, max_range, frame_step)
    if len(frame_range) < max_frames:
        frame_range = np.linspace(0, max_range - 1, max_frames).astype(int)
    start = random.randint(0, len(frame_range) - max_frames)
    # start = len(frame_range) - max_frames
    frame_range_indices = list(frame_range)[start:start + max_frames]
    frames = vr.get_batch(frame_range_indices)
    video = rearrange(frames, "f h w c -> f c h w")
    video = transform(video)
    # device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    channels = torch.load(signal_path, map_location="cuda:0", weights_only=True)
    # print("check", frame_step, start, max_frames, frame_range_indices, max_range, frame_range)

    if frame_range_indices[0] - frame_step >= 0:
        partial_channels = channels[frame_range_indices[0] - frame_step:frame_range_indices[-1], :]
        # print(partial_channels.size())  # 75, 512
        # partial_channels = partial_channels.reshape()
    else:
        partial_channels = []

        for i in range(0, len(frame_range_indices)):
            # frame_range_indices[i] 2
            # frame_range_indices[i-1 ] 0
            if i == 0:
                if frame_range_indices[i] >= frame_step:
                    tmp_channel = channels[frame_range_indices[i] - frame_step:frame_range_indices[i], :]
                    assert tmp_channel.size(0) == frame_step
                    partial_channels.append(tmp_channel)
                else:
                    tmp_channel = channels[0:frame_range_indices[i], :]

                    first_element = channels[0]
                    first_element = first_element.unsqueeze(0)
                    first_element_duplicated = first_element.repeat(frame_step - frame_range_indices[i], 1)
                    tmp_channel = torch.cat((first_element_duplicated, tmp_channel), dim=0)
                    assert tmp_channel.size(0) == frame_step, tmp_channel.size()
                    partial_channels.append(tmp_channel)
            else:
                frame_diff = frame_range_indices[i] + 1 - frame_range_indices[i - 1]

                # frame_diff 3
                if frame_diff == frame_step + 1:
                    tmp_channel = channels[frame_range_indices[i - 1]:frame_range_indices[i], :]
                    assert tmp_channel.size(0) == frame_step
                    partial_channels.append(tmp_channel)
                else:
                    tmp_channel = channels[frame_range_indices[i - 1]:frame_range_indices[i], :]

                    first_element = channels[frame_range_indices[i - 1]]
                    first_element = first_element.unsqueeze(0)
                    first_element_duplicated = first_element.repeat(frame_step - frame_diff + 1, 1)
                    tmp_channel = torch.cat((first_element_duplicated, tmp_channel), dim=0)
                    assert tmp_channel.size(0) == frame_step, tmp_channel.size()
                    partial_channels.append(tmp_channel)

            #            else:
            #                 raise Exception(frame_range_indices)

            """
            elif frame_range_indices[i] - frame_range_indices[i-1] == frame_step - 1:
                first_element = channels[frame_range_indices[i]].unsqueeze(0)
                tmp_channel = channels[frame_range_indices[i]:frame_range_indices[i+1], :]

                tmp_channel = torch.cat((first_element, tmp_channel), dim=0)
                assert tmp_channel.size(0) == frame_step
                partial_channels.append(tmp_channel)
            """

        partial_channels = torch.cat(partial_channels, dim=0)

    # print(partial_channels.size()) # 75, 512

    # partial_channels = np.array(0)
    return video, partial_channels, frame_step


def process_video(vid_path, use_bucketing, w, h, get_frame_buckets, get_frame_batch):
    if use_bucketing:
        vr = decord.VideoReader(vid_path)
        resize = get_frame_buckets(vr)
        video = get_frame_batch(vr, resize=resize)
    else:
        vr = decord.VideoReader(vid_path)
        video = get_frame_batch(vr)

    return video, vr


# https://github.com/ExponentialML/Video-BLIP2-Preprocessor
class VideoBLIPDataset(Dataset):
    def __init__(
            self,
            tokenizer=None,
            width: int = 256,
            height: int = 256,
            n_sample_frames: int = 4,
            sample_start_idx: int = 1,
            fps: int = 1,
            json_path: str = "",
            json_data=None,
            vid_data_key: str = "video_path",
            sig_data_key: str = "signal_path",
            preprocessed: bool = False,
            use_bucketing: bool = False,
            motion_threshold=50,
            **kwargs
    ):
        self.vid_types = (".mp4", ".avi", ".mov", ".webm", ".flv", ".mjpeg")
        self.use_bucketing = use_bucketing
        self.tokenizer = tokenizer
        self.preprocessed = preprocessed

        self.vid_data_key = vid_data_key
        self.sig_data_key = sig_data_key

        self.train_data = self.load_from_json(json_path, json_data)
        self.motion_threshold = motion_threshold
        self.width = width
        self.height = height

        self.n_sample_frames = n_sample_frames
        self.sample_start_idx = sample_start_idx
        self.fps = fps
        self.transform = T.Compose([
            # T.RandomResizedCrop(size=(height, width), scale=(0.8, 1.0), ratio=(width/height, width/height), antialias=False)
            T.Resize(min(height, width), antialias=False),
            T.CenterCrop([height, width])
        ])

    def build_json(self, json_data):
        extended_data = []
        for data in json_data['data']:
            for nested_data in data['data']:
                self.build_json_dict(
                    data,
                    nested_data,
                    extended_data
                )
        json_data = extended_data
        return json_data

    def build_json_dict(self, data, nested_data, extended_data):
        clip_path = nested_data['clip_path'] if 'clip_path' in nested_data else None

        extended_data.append({
            self.vid_data_key: data[self.vid_data_key],
            self.sig_data_key: data[self.sig_data_key],
            'frame_index': nested_data['frame_index'],
            'prompt': nested_data['prompt'],
            'clip_path': clip_path
        })

    def load_from_json(self, path, json_data):
        try:
            with open(path) as jpath:
                print(f"Loading JSON from {path}")
                json_data = json.load(jpath)

                return self.build_json(json_data)

        except:
            import traceback
            traceback.print_exc()
            self.train_data = []
            print("Non-existant JSON path. Skipping.")

    def validate_json(self, base_path, path):
        return os.path.exists(f"{base_path}/{path}")

    def get_frame_buckets(self, vr):
        _, h, w = vr[0].shape
        width, height = sensible_buckets(self.width, self.height, h, w)
        resize = T.transforms.Resize((height, width), antialias=True)

        return resize

    def train_data_batch(self, index):
        vid_data = self.train_data[index]
        # Get video prompt
        prompt = vid_data['prompt']
        # If we are training on individual clips.
        if 'clip_path' in self.train_data[index] and \
                self.train_data[index]['clip_path'] is not None:
            clip_path = vid_data['clip_path']
        else:
            clip_path = vid_data[self.vid_data_key]
            # Get the frame of the current index.
            self.sample_start_idx = vid_data['frame_index']

        vr = decord.VideoReader(clip_path)

        video, signal = get_frame_signal_batch(vid_data[self.sig_data_key], self.n_sample_frames, self.fps, vr,
                                               self.transform)
        # video = get_frame_batch(self.n_sample_frames, self.fps, vr, self.transform)

        # prompt_ids = np.array(0)
        # prompt = np.array(0)
        prompt_ids = get_prompt_ids(prompt, self.tokenizer)

        example = {
            "pixel_values": normalize_input(video),
            "signal_values": signal,
            "prompt_ids": prompt_ids,
            "text_prompt": prompt,
            'dataset': self.__getname__(),
        }
        # mask = np.array(0)
        mask = get_moved_area_mask(video.permute([0, 2, 3, 1]).numpy())
        example['mask'] = mask
        # example['motion'] = np.array(0)
        example['motion'] = calculate_motion_score(video.permute([0, 2, 3, 1]).numpy())

        return example

    def train_data_sig_agg_batch(self, index):
        vid_data = self.train_data[index]
        # Get video prompt
        prompt = vid_data['prompt']
        # If we are training on individual clips.
        if 'clip_path' in self.train_data[index] and \
                self.train_data[index]['clip_path'] is not None:
            clip_path = vid_data['clip_path']
        else:
            clip_path = vid_data[self.vid_data_key]
            # Get the frame of the current index.
            self.sample_start_idx = vid_data['frame_index']

        vr = decord.VideoReader(clip_path)

        video, signal, frame_step = get_frame_agg_signal_batch(vid_data[self.sig_data_key], self.n_sample_frames,
                                                               self.fps, vr, self.transform)
        # video = get_frame_batch(self.n_sample_frames, self.fps, vr, self.transform)

        # prompt_ids = np.array(0)
        # prompt = np.array(0)
        prompt_ids = get_prompt_ids(prompt, self.tokenizer)

        example = {
            "pixel_values": normalize_input(video),
            "signal_values": signal,
            "prompt_ids": prompt_ids,
            "text_prompt": prompt,
            "frame_step": frame_step,
            'dataset': self.__getname__(),
        }
        # mask = np.array(0)
        mask = get_moved_area_mask(video.permute([0, 2, 3, 1]).numpy())
        example['mask'] = mask
        # example['motion'] = np.array(0)
        example['motion'] = calculate_motion_score(video.permute([0, 2, 3, 1]).numpy())

        return example

    @staticmethod
    def __getname__():
        return 'video_blip'

    def __len__(self):
        if self.train_data is not None:
            return len(self.train_data)
        else:
            return 0

    def __getitem__(self, index):
        example = self.train_data_batch(index)
        if example['motion'] < self.motion_threshold:
            return self.__getitem__(random.randint(0, len(self) - 1))
        return example


class VideoBLIPDataset_V2(Dataset):
    def __init__(
            self,
            tokenizer=None,
            width: int = 256,
            height: int = 256,
            n_sample_frames: int = 4,
            sample_start_idx: int = 1,
            fps: int = 1,
            n_input_frames=1,
            json_path: str = "",
            json_data=None,
            vid_data_key: str = "video_path",
            sig_data_key: str = "signal_path",
            preprocessed: bool = False,
            use_bucketing: bool = False,
            motion_threshold=50,
            **kwargs
    ):
        self.vid_types = (".mp4", ".avi", ".mov", ".webm", ".flv", ".mjpeg")
        self.use_bucketing = use_bucketing
        self.tokenizer = tokenizer
        self.preprocessed = preprocessed

        self.vid_data_key = vid_data_key
        self.sig_data_key = sig_data_key

        self.train_data = self.load_from_json(json_path, json_data)
        self.motion_threshold = motion_threshold
        self.width = width
        self.height = height

        self.n_sample_frames = n_sample_frames
        self.sample_start_idx = sample_start_idx
        self.fps = fps
        self.n_input_frames = n_input_frames

        self.transform = T.Compose([
            # T.RandomResizedCrop(size=(height, width), scale=(0.8, 1.0), ratio=(width/height, width/height), antialias=False)
            T.Resize(min(height, width), antialias=False),
            T.CenterCrop([height, width])
        ])

    def build_json(self, json_data):
        extended_data = []
        for data in json_data['data']:
            for nested_data in data['data']:
                self.build_json_dict(
                    data,
                    nested_data,
                    extended_data
                )
        json_data = extended_data
        return json_data

    def build_json_dict(self, data, nested_data, extended_data):
        clip_path = nested_data['clip_path'] if 'clip_path' in nested_data else None

        extended_data.append({
            self.vid_data_key: data[self.vid_data_key],
            self.sig_data_key: data[self.sig_data_key],
            'frame_index': nested_data['frame_index'],
            'prompt': nested_data['prompt'],
            'clip_path': clip_path
        })

    def load_from_json(self, path, json_data):
        try:
            with open(path) as jpath:
                print(f"Loading JSON from {path}")
                json_data = json.load(jpath)

                return self.build_json(json_data)

        except:
            import traceback
            traceback.print_exc()
            self.train_data = []
            print("Non-existant JSON path. Skipping.")

    def validate_json(self, base_path, path):
        return os.path.exists(f"{base_path}/{path}")

    def get_frame_buckets(self, vr):
        _, h, w = vr[0].shape
        width, height = sensible_buckets(self.width, self.height, h, w)
        resize = T.transforms.Resize((height, width), antialias=True)

        return resize

    def train_data_sig_agg_batch(self, index):
        vid_data = self.train_data[index]
        # Get video prompt
        prompt = vid_data['prompt']
        # If we are training on individual clips.
        if 'clip_path' in self.train_data[index] and \
                self.train_data[index]['clip_path'] is not None:
            clip_path = vid_data['clip_path']
        else:
            clip_path = vid_data[self.vid_data_key]
            # Get the frame of the current index.
            self.sample_start_idx = vid_data['frame_index']

        vr = decord.VideoReader(clip_path)

        video, signal, frame_step = get_frame_agg_signal_batch(vid_data[self.sig_data_key], self.n_sample_frames+self.n_input_frames,
                                                               self.fps, vr, self.transform)
        # video = get_frame_batch(self.n_sample_frames, self.fps, vr, self.transform)

        # prompt_ids = np.array(0)
        # prompt = np.array(0)
        prompt_ids = get_prompt_ids(prompt, self.tokenizer)

        example = {
            "pixel_values": normalize_input(video),
            "signal_values": signal,
            "prompt_ids": prompt_ids,
            "text_prompt": prompt,
            "frame_step": frame_step,
            'dataset': self.__getname__(),
        }
        # mask = np.array(0)
        mask = get_moved_area_mask(video.permute([0, 2, 3, 1]).numpy())
        example['mask'] = mask
        # example['motion'] = np.array(0)
        example['motion'] = calculate_motion_score(video.permute([0, 2, 3, 1]).numpy())

        return example

    @staticmethod
    def __getname__():
        return 'video_blip_v2'

    def __len__(self):
        if self.train_data is not None:
            return len(self.train_data)
        else:
            return 0

    def __getitem__(self, index):
        example = self.train_data_sig_agg_batch(index)
        if example['motion'] < self.motion_threshold:
            return self.__getitem__(random.randint(0, len(self) - 1))
        return example


class SingleVideoDataset(Dataset):
    def __init__(
            self,
            tokenizer=None,
            width: int = 256,
            height: int = 256,
            n_sample_frames: int = 4,
            frame_step: int = 1,
            single_video_path: str = "",
            single_video_prompt: str = "",
            use_caption: bool = False,
            use_bucketing: bool = False,
            **kwargs
    ):
        self.tokenizer = tokenizer
        self.use_bucketing = use_bucketing
        self.frames = []
        self.index = 1

        self.vid_types = (".mp4", ".avi", ".mov", ".webm", ".flv", ".mjpeg")
        self.n_sample_frames = n_sample_frames
        self.frame_step = frame_step

        self.single_video_path = single_video_path
        self.single_video_prompt = single_video_prompt

        self.width = width
        self.height = height

    def create_video_chunks(self):
        # Create a list of frames separated by sample frames
        # [(1,2,3), (4,5,6), ...]
        vr = decord.VideoReader(self.single_video_path)
        vr_range = range(1, len(vr), self.frame_step)

        self.frames = list(self.chunk(vr_range, self.n_sample_frames))

        # Delete any list that contains an out of range index.
        for i, inner_frame_nums in enumerate(self.frames):
            for frame_num in inner_frame_nums:
                if frame_num > len(vr):
                    print(f"Removing out of range index list at position: {i}...")
                    del self.frames[i]

        return self.frames

    def chunk(self, it, size):
        it = iter(it)
        return iter(lambda: tuple(islice(it, size)), ())

    def get_frame_batch(self, vr, resize=None):
        index = self.index
        frames = vr.get_batch(self.frames[self.index])
        video = rearrange(frames, "f h w c -> f c h w")

        if resize is not None: video = resize(video)
        return video

    def get_frame_buckets(self, vr):
        _, h, w = vr[0].shape
        width, height = sensible_buckets(self.width, self.height, h, w)
        resize = T.transforms.Resize((height, width), antialias=True)

        return resize

    def process_video_wrapper(self, vid_path):
        video, vr = process_video(
            vid_path,
            self.use_bucketing,
            self.width,
            self.height,
            self.get_frame_buckets,
            self.get_frame_batch
        )

        return video, vr

    def single_video_batch(self, index):
        train_data = self.single_video_path
        self.index = index

        if train_data.endswith(self.vid_types):
            video, _ = self.process_video_wrapper(train_data)

            prompt = self.single_video_prompt
            prompt_ids = get_prompt_ids(prompt, self.tokenizer)

            return video, prompt, prompt_ids
        else:
            raise ValueError(f"Single video is not a video type. Types: {self.vid_types}")

    @staticmethod
    def __getname__():
        return 'single_video'

    def __len__(self):

        return len(self.create_video_chunks())

    def __getitem__(self, index):

        video, prompt, prompt_ids = self.single_video_batch(index)

        example = {
            "pixel_values": normalize_input(video),
            "prompt_ids": prompt_ids,
            "text_prompt": prompt,
            'dataset': self.__getname__()
        }

        return example


class ImageDataset(Dataset):

    def __init__(
            self,
            tokenizer=None,
            width: int = 256,
            height: int = 256,
            base_width: int = 256,
            base_height: int = 256,
            use_caption: bool = False,
            image_dir: str = '',
            single_img_prompt: str = '',
            use_bucketing: bool = False,
            fallback_prompt: str = '',
            **kwargs
    ):
        self.tokenizer = tokenizer
        self.img_types = (".png", ".jpg", ".jpeg", '.bmp')
        self.use_bucketing = use_bucketing
        # self.image_dir = self.get_images_list(image_dir)
        self.image_dir_path = image_dir
        self.image_dir = json.load(open(kwargs['image_json']))
        self.fallback_prompt = fallback_prompt

        self.use_caption = use_caption
        self.single_img_prompt = single_img_prompt

        self.width = width
        self.height = height

    def get_images_list(self, image_dir):
        if os.path.exists(image_dir):
            imgs = [x for x in os.listdir(image_dir) if x.endswith(self.img_types)]
            full_img_dir = []

            for img in imgs:
                full_img_dir.append(f"{image_dir}/{img}")

            return sorted(full_img_dir)

        return ['']

    def image_batch(self, index):
        train_data = self.image_dir[index]
        img, prompt = train_data['image'], train_data['caption']
        img = os.path.join(self.image_dir_path, img)
        try:
            img = torchvision.io.read_image(img, mode=torchvision.io.ImageReadMode.RGB)
        except:
            img = T.transforms.PILToTensor()(Image.open(img).convert("RGB"))

        width = self.width
        height = self.height

        if self.use_bucketing:
            _, h, w = img.shape
            width, height = sensible_buckets(width, height, w, h)

        resize = T.transforms.Resize((height, width), antialias=True)

        img = resize(img)
        img = repeat(img, 'c h w -> f c h w', f=1)
        prompt_ids = get_prompt_ids(prompt, self.tokenizer)

        return img, prompt, prompt_ids

    @staticmethod
    def __getname__():
        return 'image'

    def __len__(self):
        # Image directory
        return len(self.image_dir)

    def __getitem__(self, index):
        img, prompt, prompt_ids = self.image_batch(index)
        example = {
            "pixel_values": normalize_input(img),
            "frames": img,
            "prompt_ids": prompt_ids,
            "text_prompt": prompt,
            'dataset': self.__getname__()
        }

        return example


class VideoFolderDataset(Dataset):
    def __init__(
            self,
            tokenizer=None,
            width: int = 256,
            height: int = 256,
            n_sample_frames: int = 16,
            fps: int = 8,
            path: str = "./data",
            fallback_prompt: str = "",
            use_bucketing: bool = False,
            **kwargs
    ):
        self.tokenizer = tokenizer
        self.use_bucketing = use_bucketing

        self.fallback_prompt = fallback_prompt

        # self.video_files = glob(f"{path}/*.mp4")
        self.video_files = []

        for video_path in glob(os.path.join(self.video_directory, '**', '*.mp4'), recursive=True):
            signal_path = video_path.replace("locomotion_video_whole_mp4", "locomotion_signal_video")
            signal_path = signal_path.replace("result.mp4", "channels_0.pt")
            if os.path.exists(signal_path):
                self.video_files.append(video_path)
        # Check signal + video

        self.width = width
        self.height = height

        self.n_sample_frames = n_sample_frames
        self.fps = fps

    def get_frame_buckets(self, vr):
        _, h, w = vr[0].shape
        width, height = sensible_buckets(self.width, self.height, h, w)
        resize = T.transforms.Resize((height, width), antialias=True)

        return resize

    def get_frame_batch(self, vr, resize=None):
        n_sample_frames = self.n_sample_frames
        native_fps = vr.get_avg_fps()

        every_nth_frame = max(1, round(native_fps / self.fps))
        every_nth_frame = min(len(vr), every_nth_frame)

        effective_length = len(vr) // every_nth_frame
        if effective_length < n_sample_frames:
            n_sample_frames = effective_length
            raise RuntimeError("not enough frames")

        effective_idx = random.randint(0, (effective_length - n_sample_frames))
        idxs = every_nth_frame * np.arange(effective_idx, effective_idx + n_sample_frames)

        video = vr.get_batch(idxs)
        video = rearrange(video, "f h w c -> f c h w")

        if resize is not None: video = resize(video)
        return video, vr

    def process_video_wrapper(self, vid_path):
        video, vr, idxs = self.process_video(
            vid_path,
            self.use_bucketing,
            self.width,
            self.height,
            self.get_frame_buckets,
            self.get_frame_batch
        )
        return video, vr, idxs

    @staticmethod
    def __getname__():
        return 'folder'

    def __len__(self):
        return len(self.video_files)

    def __getitem__(self, index):
        try:
            video, _, idxs = self.process_video_wrapper(self.video_files[index])
        except Exception as err:
            print("read video error", self.video_files[index])
            video, _, idxs = self.process_video_wrapper(self.video_files[index + 1])

        # Get signal data
        signal_output = []
        if os.path.exists(self.video_files[index].replace("locomotion_video_whole_mp4", "locomotion_signal_video")):
            signal_path = self.video_files[index].replace("locomotion_video_whole_mp4", "locomotion_signal_video")
            for frame_idx in range(idxs):
                channels = torch.load(signal_path.replace("result.mp4", "channels_" + str(frame_idx) + ".pt"),
                                      weights_only=True)
                sim_output = torch.stack(channels)
                signal_output.append(sim_output)

        if os.path.exists(self.video_files[index].replace(".mp4", ".txt")):
            with open(self.video_files[index].replace(".mp4", ".txt"), "r") as f:
                lines = f.readlines()
                prompt = random.choice(lines)
        else:
            prompt = self.fallback_prompt

        prompt_ids = get_prompt_ids(prompt, self.tokenizer)

        assert signal_output

        return {"pixel_values": normalize_input(video[0]), "frames": video[0],
                "prompt_ids": prompt_ids, "text_prompt": prompt, "signals": signal_output,
                'dataset': self.__getname__()}


class VideoJsonDataset(Dataset):
    def __init__(
            self,
            tokenizer=None,
            width: int = 256,
            height: int = 256,
            n_sample_frames: int = 16,
            fps: int = 8,
            video_dir: str = "./data",
            video_json: str = "",
            fallback_prompt: str = "",
            use_bucketing: bool = False,
            motion_threshold=50,
            **kwargs
    ):
        self.tokenizer = tokenizer
        self.use_bucketing = use_bucketing

        self.fallback_prompt = fallback_prompt
        self.video_dir = video_dir
        self.video_files = json.load(open(video_json))

        self.width = width
        self.height = height

        self.n_sample_frames = n_sample_frames
        self.fps = fps
        self.motion_threshold = motion_threshold
        self.transform = T.Compose([
            # T.RandomResizedCrop(size=(height, width), scale=(0.8, 1.0), ratio=(width/height, width/height), antialias=False),
            T.Resize(min(height, width), antialias=False),
            T.CenterCrop([height, width])
        ])

    def get_frame_buckets(self, vr):
        _, h, w = vr[0].shape
        width, height = sensible_buckets(self.width, self.height, h, w)
        resize = T.transforms.Resize((height, width), antialias=True)

        return resize

    @staticmethod
    def __getname__():
        return 'video_json'

    def __len__(self):
        return len(self.video_files)

    def __getitem__(self, index):
        mask = None
        try:
            item = self.video_files[index]
            video_path = os.path.join(self.video_dir, item['video'])

            prompt = item['caption']
            if self.fallback_prompt == "<no_text>":
                prompt = ""
            vr = decord.VideoReader(video_path)
            video = get_frame_batch(self.n_sample_frames, self.fps, vr, self.transform)
        except Exception as err:
            print("read video error", err, video_path)
            return self.__getitem__(index + 1)
        prompt_ids = get_prompt_ids(prompt, self.tokenizer)

        example = {
            "pixel_values": normalize_input(video),
            "prompt_ids": prompt_ids,
            "text_prompt": prompt,
            'dataset': self.__getname__()
        }
        example['mask'] = get_moved_area_mask(video.permute([0, 2, 3, 1]).numpy())
        example['motion'] = calculate_motion_score(video.permute([0, 2, 3, 1]).numpy())
        if example['motion'] < self.motion_threshold:
            return self.__getitem__(random.randint(0, len(self) - 1))
        return example


class CachedDataset(Dataset):
    def __init__(self, cache_dir: str = ''):
        self.cache_dir = cache_dir
        self.cached_data_list = self.get_files_list()

    def get_files_list(self):
        tensors_list = [f"{self.cache_dir}/{x}" for x in os.listdir(self.cache_dir) if x.endswith('.pt')]
        return sorted(tensors_list)

    def __len__(self):
        return len(self.cached_data_list)

    def __getitem__(self, index):
        cached_latent = torch.load(self.cached_data_list[index], map_location='cuda:0', weights_only=True)
        return cached_latent


def get_train_dataset(dataset_types, train_data, tokenizer=None):
    train_datasets = []
    dataset_cls = [VideoJsonDataset, SingleVideoDataset, ImageDataset, VideoFolderDataset, VideoBLIPDataset,
                   VideoBLIPDataset_V2]
    dataset_map = {d.__getname__(): d for d in dataset_cls}

    # Loop through all available datasets, get the name, then add to list of data to process.
    for dataset in dataset_types:
        if dataset in dataset_map:
            train_datasets.append(dataset_map[dataset](**train_data, tokenizer=tokenizer))
        else:
            raise ValueError(f"Dataset type not found: {dataset} not in {dataset_map.keys()}")
    return train_datasets


def extend_datasets(datasets, dataset_items, extend=False):
    biggest_data_len = max(x.__len__() for x in datasets)
    extended = []
    for dataset in datasets:
        if dataset.__len__() == 0:
            del dataset
            continue
        if dataset.__len__() < biggest_data_len:
            for item in dataset_items:
                if extend and item not in extended and hasattr(dataset, item):
                    print(f"Extending {item}")

                    value = getattr(dataset, item)
                    value *= biggest_data_len
                    value = value[:biggest_data_len]

                    setattr(dataset, item, value)

                    print(f"New {item} dataset length: {dataset.__len__()}")
                    extended.append(item)
