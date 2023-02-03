import inspect
import random

import numpy as np
import torch
from utils import parse_key_frames, slerp

from .flow_base import BaseFlow


class GiffusionFlow(BaseFlow):
    def __init__(
        self,
        pipe,
        text_prompts,
        guidance_scale,
        num_inference_steps,
        width,
        height,
        use_fixed_latent,
        device,
        seed=42,
        batch_size=1,
        init_image=None,
    ):
        super().__init__(pipe, device, batch_size)

        self.pipe_signature = set(inspect.signature(self.pipe).parameters.keys())

        self.text_prompts = text_prompts
        self.width, self.height = width, height
        self.use_fixed_latent = use_fixed_latent
        self.guidance_scale = guidance_scale
        self.num_inference_steps = num_inference_steps
        self.seed = seed

        self.device = device
        self.generator = torch.Generator(self.device)

        self.key_frames = parse_key_frames(text_prompts)
        random.seed(self.seed)
        self.seed_schedule = {
            kf: random.randint(0, 18446744073709551615) for kf, _ in self.key_frames
        }
        last_frame, _ = max(self.key_frames, key=lambda x: x[0])
        self.max_frames = last_frame + 1
        self.init_image = init_image

        (
            self.init_latents,
            self.text_embeddings,
        ) = self.get_init_latents_and_text_embeddings(
            self.key_frames,
            self.height,
            self.width,
            self.generator,
            self.use_fixed_latent,
        )

    @torch.no_grad()
    def get_init_latents_and_text_embeddings(
        self, key_frames, height, width, generator, use_fixed_latent=False
    ):
        text_output = {}
        latent_output = {}
        start_latent = torch.randn(
            (
                1,
                self.pipe.unet.in_channels,
                height // self.pipe.vae_scale_factor,
                width // self.pipe.vae_scale_factor,
            ),
            device=self.pipe.device,
            generator=generator.manual_seed(self.seed),
        )

        for idx, (start_key_frame, end_key_frame) in enumerate(
            zip(key_frames, key_frames[1:])
        ):
            start_frame, start_prompt = start_key_frame
            end_frame, end_prompt = end_key_frame

            end_latent = (
                start_latent
                if use_fixed_latent
                else torch.randn(
                    (
                        1,
                        self.pipe.unet.in_channels,
                        height // self.pipe.vae_scale_factor,
                        width // self.pipe.vae_scale_factor,
                    ),
                    device=self.pipe.device,
                    generator=generator.manual_seed(self.seed_schedule[end_frame]),
                )
            )
            start_text_embeddings = self.prompt_to_embedding(start_prompt)
            end_text_embeddings = self.prompt_to_embedding(end_prompt)

            num_frames = (end_frame - start_frame) + 1
            interp_schedule = np.linspace(0, 1, num_frames)
            for i, t in enumerate(interp_schedule):
                latents = slerp(float(t), start_latent, end_latent)

                start_text_embeddings, end_text_embeddings = self.pad_embedding(
                    start_text_embeddings, end_text_embeddings
                )
                embeddings = slerp(float(t), start_text_embeddings, end_text_embeddings)

                latent_output[i + start_frame] = latents
                text_output[i + start_frame] = embeddings

            start_latent = end_latent

        return latent_output, text_output

    def batch_generator(self, frames, batch_size):
        text_batch = []
        latent_batch = []
        generator_batch = []

        for frame_idx in frames:
            text_batch.append(self.text_embeddings[frame_idx])
            latent_batch.append(self.init_latents[frame_idx])

            if len(text_batch) % batch_size == 0:
                text_batch = torch.cat(text_batch, dim=0)
                latent_batch = torch.cat(latent_batch, dim=0)
                generator_batch.append(
                    torch.Generator(self.device).manual_seed(
                        self.seed_schedule[frame_idx]
                    )
                )

                yield {
                    "text_embeddings": text_batch,
                    "init_latents": latent_batch,
                    "generators": generator_batch,
                }

                text_batch = []
                latent_batch = []
                generator_batch = []

    def create(self, frames=None):
        for batch in self.batch_generator(
            frames if frames else [i for i in range(self.max_frames)], self.batch_size
        ):

            prompt_embeds = batch["text_embeddings"]
            latents = batch["init_latents"]
            generators = batch["generators"]

            pipe_kwargs = dict(
                height=self.height,
                width=self.width,
                num_inference_steps=self.num_inference_steps,
                guidance_scale=self.guidance_scale,
            )

            if "image" in self.pipe_signature:
                if self.init_image is not None:
                    pipe_kwargs.update(
                        {
                            "image": self.init_image,
                            "strength": self.strength,
                            "generator": generators,
                        }
                    )

            if "latents" in self.pipe_signature:
                pipe_kwargs.update({"latents": latents})

            if "prompt_embeds" in self.pipe_signature:
                pipe_kwargs.update({"prompt_embeds": prompt_embeds})

            with torch.autocast("cuda"):
                images = self.pipe(**pipe_kwargs)

            yield images
