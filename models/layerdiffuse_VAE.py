import torch.nn as nn
import torch

from typing import Optional, Tuple
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
from diffusers.models.unet_2d_blocks import UNetMidBlock2D, get_down_block, get_up_block


# referenced from https://github.com/layerdiffusion/sd-forge-layerdiffuse/blob/main/lib_layerdiffusion/models.py

def zero_module(module):
    for p in module.parameters():
        p.detach().zero_()
    return module


class LatentTransparencyOffsetEncoder(torch.nn.Module):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.blocks = torch.nn.Sequential(
            torch.nn.Conv2d(4, 32, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(32, 32, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(32, 64, kernel_size=3, padding=1, stride=2),
            nn.SiLU(),
            torch.nn.Conv2d(64, 64, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(64, 128, kernel_size=3, padding=1, stride=2),
            nn.SiLU(),
            torch.nn.Conv2d(128, 128, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            torch.nn.Conv2d(128, 256, kernel_size=3, padding=1, stride=2),
            nn.SiLU(),
            torch.nn.Conv2d(256, 256, kernel_size=3, padding=1, stride=1),
            nn.SiLU(),
            zero_module(torch.nn.Conv2d(256, 4, kernel_size=3, padding=1, stride=1)),
        )

    def __call__(self, x):
        return self.blocks(x)


class ImageResizeEncoder(torch.nn.Module):
    def __init__(self, input_dim, output_dim, hidden_dim=1024):
        super(ImageResizeEncoder, self).__init__()
        self.blocks = torch.nn.Sequential(
            nn.Linear(input_dim, hidden_dim, device='cuda'),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim, device='cuda'),
            nn.ReLU(),
            nn.Linear(hidden_dim, output_dim, device='cuda'),
        )

    def __call__(self, x):
        return self.blocks(x)


class SignalResizeEncoder(torch.nn.Module):
    def __init__(self, input_dim, output_dim, hidden_dim=1024):
        super(SignalResizeEncoder, self).__init__()

        self.encoder = torch.nn.Sequential(
            nn.Linear(input_dim, hidden_dim, device='cuda'),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim, device='cuda'),
            nn.ReLU(),
            nn.Linear(hidden_dim, output_dim, device='cuda'),
        )

    def forward(self, x):
        # x shape: (batch_size, 25, 512)
        x = self.encoder(x)  # Pass input through the multi-layer encoder
        return x


class LatentSignalEncoder(torch.nn.Module):
    def __init__(self, input_dim=512, hidden_dims=[1024, 512, 256, 128, 64], output_dim=32, dropout_prob=0.3):
        super(LatentSignalEncoder, self).__init__()

        # Create a list of layers
        layers = []
        current_dim = input_dim

        # Add hidden layers
        for hidden_dim in hidden_dims:
            layers.append(nn.Linear(current_dim, hidden_dim, device='cuda'))  # Linear layer
            layers.append(nn.ReLU())  # ReLU activation function
            # layers.append(nn.Dropout(p=dropout_prob))  # Dropout layer
            current_dim = hidden_dim

        # Add the final output layer
        layers.append(nn.Linear(current_dim, output_dim, device='cuda'))

        # Use nn.Sequential to combine the layers into a single module
        self.encoder = nn.Sequential(*layers)

    def forward(self, x):
        # x shape: (batch_size, 25, 512)
        x = self.encoder(x)  # Pass input through the multi-layer encoder
        return x


class LatentSignal2DEncoder(torch.nn.Module):
    def __init__(self, input_dim=512, hidden_dims=[1024, 512, 256, 128, 64], output_dim=32, dropout_prob=0.3):
        super(LatentSignal2DEncoder, self).__init__()

        # Create a list of layers
        layers = []
        current_dim = input_dim

        # Add hidden layers
        for hidden_dim in hidden_dims:
            layers.append(nn.Linear(current_dim, hidden_dim, device='cuda'))  # Linear layer
            layers.append(nn.ReLU())  # ReLU activation function
            # layers.append(nn.Dropout(p=dropout_prob))  # Dropout layer
            current_dim = hidden_dim

        # Add the final output layer
        layers.append(nn.Linear(current_dim, output_dim, device='cuda'))

        # Use nn.Sequential to combine the layers into a single module
        self.encoder = nn.Sequential(*layers)

    def forward(self, x):
        # x shape: (batch_size, 25, 512)
        x = self.encoder(x)  # Pass input through the multi-layer encoder
        return x


class SignalEncoder(nn.Module):
    def __init__(self, input_size=512, output_size=1024):
        super(SignalEncoder, self).__init__()
        # We first reduce the frame channel dimension (3) with a convolution
        self.conv1 = nn.Conv1d(in_channels=3, out_channels=8, kernel_size=1)
        # Flattening the reduced signal for the next layer
        self.fc1 = nn.Linear(8 * input_size, output_size)
        # Optional non-linearity
        self.relu = nn.ReLU()

    def forward(self, x):
        # x has shape (batch_size, frames, frame_channel, signal_data)
        batch_size, frames, frame_channel, signal_data = x.size()

        # Apply convolution along the frame_channel dimension
        x = x.view(batch_size * frames, frame_channel, signal_data)  # Reshape for Conv1d
        x = self.conv1(x)  # Shape: (batch_size * frames, out_channels=8, signal_data)

        # Flatten the convolution output to apply the linear layer
        x = x.view(batch_size * frames, -1)  # Shape: (batch_size * frames, 8 * signal_data)

        # Apply the fully connected layer to get the desired output size
        x = self.fc1(x)  # Shape: (batch_size * frames, output_size=1024)

        # Optional activation
        x = self.relu(x)

        # Reshape back to (batch_size, frames, output_size)
        x = x.view(batch_size, frames, -1)

        return x


class SignalEncoder2(nn.Module):
    def __init__(self, signal_data_dim, target_h, target_w):
        super(SignalEncoder2, self).__init__()
        self.conv1 = nn.Conv1d(in_channels=3, out_channels=64, kernel_size=3, padding=1)
        self.conv2 = nn.Conv1d(in_channels=64, out_channels=128, kernel_size=3, padding=1)
        self.fc = nn.Linear(128 * signal_data_dim, target_h * target_w)
        self.target_h = target_h
        self.target_w = target_w

    def forward(self, x):
        batch_size, frames, channels, signal_data = x.shape

        # Reshape for Conv1D: (batch_size * frames, channels, signal_data)
        x = x.view(batch_size * frames, channels, signal_data)

        # Apply Conv1D layers
        x = self.conv1(x)
        x = torch.relu(x)
        x = self.conv2(x)
        x = torch.relu(x)

        # Flatten and apply the fully connected layer to get the desired h and w
        x = x.view(batch_size * frames, -1)  # Flatten the conv output
        x = self.fc(x)

        # Reshape to (batch_size, frames, 1, h, w)
        x = x.view(batch_size, frames, 1, self.target_h, self.target_w)

        return x

class UNet384(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
            self,
            in_channels: int = 3,
            out_channels: int = 4,
            down_block_types: Tuple[str] = ("DownBlock2D", "DownBlock2D", "DownBlock2D", "AttnDownBlock2D"),
            up_block_types: Tuple[str] = ("AttnUpBlock2D", "UpBlock2D", "UpBlock2D", "UpBlock2D"),
            block_out_channels: Tuple[int] = (32, 64, 128, 256),
            layers_per_block: int = 2,
            mid_block_scale_factor: float = 1,
            downsample_padding: int = 1,
            downsample_type: str = "conv",
            upsample_type: str = "conv",
            dropout: float = 0.0,
            act_fn: str = "silu",
            attention_head_dim: Optional[int] = 8,
            norm_num_groups: int = 4,
            norm_eps: float = 1e-5,
    ):
        super().__init__()

        # input
        self.conv_in = nn.Conv2d(in_channels, block_out_channels[0], kernel_size=3, padding=(1, 1))
        self.latent_conv_in = zero_module(nn.Conv2d(4, block_out_channels[2], kernel_size=1))

        self.down_blocks = nn.ModuleList([])
        self.mid_block = None
        self.up_blocks = nn.ModuleList([])

        # down
        output_channel = block_out_channels[0]
        for i, down_block_type in enumerate(down_block_types):
            input_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == len(block_out_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=layers_per_block,
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=None,
                add_downsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=attention_head_dim if attention_head_dim is not None else output_channel,
                downsample_padding=downsample_padding,
                resnet_time_scale_shift="default",
                downsample_type=downsample_type,
                dropout=dropout,
            )
            self.down_blocks.append(down_block)

        # mid
        self.mid_block = UNetMidBlock2D(
            in_channels=block_out_channels[-1],
            temb_channels=None,
            dropout=dropout,
            resnet_eps=norm_eps,
            resnet_act_fn=act_fn,
            output_scale_factor=mid_block_scale_factor,
            resnet_time_scale_shift="default",
            attention_head_dim=attention_head_dim if attention_head_dim is not None else block_out_channels[-1],
            resnet_groups=norm_num_groups,
            attn_groups=None,
            add_attention=True,
        )

        # up
        reversed_block_out_channels = list(reversed(block_out_channels))
        output_channel = reversed_block_out_channels[0]
        for i, up_block_type in enumerate(up_block_types):
            prev_output_channel = output_channel
            output_channel = reversed_block_out_channels[i]
            input_channel = reversed_block_out_channels[min(i + 1, len(block_out_channels) - 1)]

            is_final_block = i == len(block_out_channels) - 1

            up_block = get_up_block(
                up_block_type,
                num_layers=layers_per_block + 1,
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=None,
                add_upsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=attention_head_dim if attention_head_dim is not None else output_channel,
                resnet_time_scale_shift="default",
                upsample_type=upsample_type,
                dropout=dropout,
            )
            self.up_blocks.append(up_block)
            prev_output_channel = output_channel

        # out
        self.conv_norm_out = nn.GroupNorm(num_channels=block_out_channels[0], num_groups=norm_num_groups, eps=norm_eps)
        self.conv_act = nn.SiLU()
        self.conv_out = nn.Conv2d(block_out_channels[0], out_channels, kernel_size=3, padding=1)

    def forward(self, x, latent):
        sample_latent = self.latent_conv_in(latent)
        sample = self.conv_in(x)
        emb = None

        down_block_res_samples = (sample,)
        for i, downsample_block in enumerate(self.down_blocks):
            # 8X downsample
            if i == 3:
                sample = sample + sample_latent

            sample, res_samples = downsample_block(hidden_states=sample, temb=emb)
            down_block_res_samples += res_samples

        assert len(self.down_blocks) == 4

        sample = self.mid_block(sample, emb)

        for upsample_block in self.up_blocks:
            res_samples = down_block_res_samples[-len(upsample_block.resnets):]
            down_block_res_samples = down_block_res_samples[: -len(upsample_block.resnets)]
            sample = upsample_block(sample, res_samples, emb)

        sample = self.conv_norm_out(sample)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)
        return sample

    def __call__(self, x, latent):
        return self.forward(x, latent)
