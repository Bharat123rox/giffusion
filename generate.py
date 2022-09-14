import inspect
import os
import uuid
from datetime import datetime

import numpy as np
import torch
import torchvision
import torchvision.transforms.functional as F
import typer
from diffusers import StableDiffusionPipeline
from diffusers.schedulers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler
from PIL import Image
from torch import autocast, embedding
from torchvision import transforms as T
from tqdm import tqdm

from comet import start_experiment
from safety_checker import StableDiffusionSafetyChecker
from utils import parse_key_frames, slerp

PRETRAINED_MODEL_NAME = os.getenv(
    "PRETRAINED_MODEL_NAME", "CompVis/stable-diffusion-v1-4"
)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
pipe = StableDiffusionPipeline.from_pretrained(
    PRETRAINED_MODEL_NAME, use_auth_token=True
)
pipe.to(device)

SCHEDULERS = dict(
    pndms=PNDMScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        skip_prk_steps=True,
    ),
    ddim=DDIMScheduler(
        beta_start=0.00085,
        beta_end=0.012,
        beta_schedule="scaled_linear",
        clip_sample=False,
        set_alpha_to_one=False,
    ),
    klms=LMSDiscreteScheduler(
        beta_start=0.00085, beta_end=0.012, beta_schedule="scaled_linear"
    ),
)


experiment = start_experiment()


def save_gif(frames, filename="./output.gif", fps=24):
    imgs = [Image.open(f) for f in sorted(frames)]
    imgs += imgs[-1:1:-1]
    duration = len(imgs) // fps
    imgs[0].save(
        fp=filename,
        format="GIF",
        append_images=imgs[1:],
        save_all=True,
        duration=duration,
        loop=1,
    )


def postprocess(images):
    images = (images / 2 + 0.5).clamp(0, 1)
    images = (images * 255).to(torch.uint8)
    images = images.permute(0, 2, 3, 1)
    images = images.cpu().numpy()
    images = numpy_to_pil(images)

    return images


def numpy_to_pil(images):
    """
    Convert a numpy image or a batch of images to a PIL image.
    """
    if images.ndim == 3:
        images = images[None, ...]
    pil_images = [Image.fromarray(image) for image in images]

    return pil_images


@torch.no_grad()
def denoise(latents, pipe, text_embeddings, i, t, guidance_scale):
    accepts_eta = "eta" in set(inspect.signature(pipe.scheduler.step).parameters.keys())
    extra_step_kwargs = {}
    if accepts_eta:
        extra_step_kwargs["eta"] = 0.0

    latent_model_input = torch.cat([latents] * text_embeddings.shape[0])

    noise_pred = pipe.unet(
        latent_model_input, t, encoder_hidden_states=text_embeddings
    )["sample"]

    pred_decomp = noise_pred.chunk(text_embeddings.shape[0])
    noise_pred_uncond, noise_pred_cond = pred_decomp[0], torch.cat(
        pred_decomp[1:], dim=0
    ).mean(dim=0, keepdim=True)
    noise_pred = noise_pred_uncond + guidance_scale * (
        noise_pred_cond - noise_pred_uncond
    )
    if isinstance(pipe.scheduler, LMSDiscreteScheduler):
        latents = pipe.scheduler.step(noise_pred, i, latents, **extra_step_kwargs)[
            "prev_sample"
        ]
    else:
        latents = pipe.scheduler.step(noise_pred, t, latents, **extra_step_kwargs)[
            "prev_sample"
        ]

    return latents


@torch.no_grad()
def diffuse_latents(
    pipe,
    cond_embeddings,
    cond_latents,
    num_inference_steps=50,
    guidance_scale=7.5,
    offset=1,
    eta=0.0,
):

    batch_size = 1

    # set timesteps
    accepts_offset = "offset" in set(
        inspect.signature(pipe.scheduler.set_timesteps).parameters.keys()
    )
    extra_set_kwargs = {}
    if accepts_offset:
        extra_set_kwargs["offset"] = offset
    pipe.scheduler.set_timesteps(num_inference_steps, **extra_set_kwargs)

    if isinstance(pipe.scheduler, LMSDiscreteScheduler):
        cond_latents = cond_latents * pipe.scheduler.sigmas[0]

    accepts_eta = "eta" in set(inspect.signature(pipe.scheduler.step).parameters.keys())
    extra_step_kwargs = {}
    if accepts_eta:
        extra_step_kwargs["eta"] = eta

    max_length = cond_embeddings.shape[1]
    uncond_input = pipe.tokenizer(
        [""] * batch_size,
        padding="max_length",
        max_length=max_length,
        return_tensors="pt",
    )
    uncond_embeddings = pipe.text_encoder(uncond_input.input_ids.to(device))[0]
    text_embeddings = torch.cat([uncond_embeddings, cond_embeddings])

    latents = cond_latents
    for i, t in enumerate(pipe.scheduler.timesteps):
        if isinstance(pipe.scheduler, LMSDiscreteScheduler):
            sigma = pipe.scheduler.sigmas[i]
            # the model input needs to be scaled to match the continuous ODE formulation in K-LMS
            latents = latents / ((sigma**2 + 1) ** 0.5)

        latents = denoise(latents, pipe, text_embeddings, i, t, guidance_scale)

    return latents


@torch.no_grad()
def latents_to_image(pipe, latents):
    # scale and decode the image latents with vae
    latents = 1 / 0.18215 * latents
    images = pipe.vae.decode(latents).sample
    images = postprocess(images)

    return images


@torch.no_grad()
def prompt_to_embedding(pipe, prompt):
    if "|" in prompt:
        prompt = [x.strip() for x in prompt.split("|")]
    text_inputs = pipe.tokenizer(
        prompt,
        padding="max_length",
        max_length=pipe.tokenizer.model_max_length,
        truncation=True,
        return_tensors="pt",
    )
    text_inputs = text_inputs.input_ids.to(pipe.text_encoder.device)
    text_embeddings = pipe.text_encoder(text_inputs)[0]

    return text_embeddings


@torch.no_grad()
def interpolate_latents_and_text_embeddings(key_frames, pipe, height, width, generator):
    text_output = {}
    latent_output = {}

    start_key_frame, *key_frames = key_frames
    start_frame_idx, start_prompt = start_key_frame

    start_latent = torch.randn(
        (1, pipe.unet.in_channels, height // 8, width // 8),
        device=pipe.device,
        generator=generator,
    )
    start_text_embeddings = prompt_to_embedding(pipe, start_prompt)

    for key_frame in key_frames:
        current_frame_idx, current_prompt = key_frame

        current_latent = torch.randn(
            (1, pipe.unet.in_channels, height // 8, width // 8),
            device=pipe.device,
            generator=generator,
        )
        current_text_embeddings = prompt_to_embedding(pipe, current_prompt)

        num_steps = current_frame_idx - start_frame_idx
        for i, t in enumerate(np.linspace(0, 1, num_steps + 1)):
            latents = slerp(float(t), start_latent, current_latent)
            embeddings = slerp(float(t), start_text_embeddings, current_text_embeddings)

            latent_output[i + start_frame_idx] = latents
            text_output[i + start_frame_idx] = embeddings

        start_latent = current_latent
        start_text_embeddings = current_text_embeddings

        start_frame_idx = current_frame_idx

    return latent_output, text_output


def run(
    text_prompt_inputs,
    num_inference_steps=50,
    guidance_scale=7.5,
    seed=42,
    fps=24,
    scheduler="pndms",
):
    if experiment:
        run_path = f"../generated/{experiment.name}"
    else:
        run_path = f"../generated/{datetime.today().strftime('%Y-%m-%d-%H:%M:%S')}"

    os.makedirs(run_path, exist_ok=True)

    if experiment:
        experiment.log_parameters(
            {
                "text_prompt_inputs": text_prompt_inputs,
                "num_inference_steps": num_inference_steps,
                "guidance_scale": guidance_scale,
                "scheduler": scheduler,
                "seed": seed,
                "fps": fps,
            }
        )
    pipe.scheduler = SCHEDULERS.get(scheduler)

    generator = torch.Generator(device=device).manual_seed(int(seed))

    key_frames = parse_key_frames(text_prompt_inputs)
    max_frames = max(key_frames, key=lambda x: x[0])[0]

    init_latents, text_embeddings = interpolate_latents_and_text_embeddings(
        key_frames, pipe, 512, 512, generator
    )

    output_frames = []
    for frame_idx in tqdm(range(max_frames + 1), total=max_frames + 1):
        init_latent = init_latents[frame_idx]
        text_embedding = text_embeddings[frame_idx]

        init_latent = init_latent.to(device)
        text_embedding = text_embedding.to(device)

        with autocast("cuda"):
            latents = diffuse_latents(
                pipe,
                text_embedding,
                init_latent,
                num_inference_steps,
                guidance_scale,
            )
            images = latents_to_image(pipe, latents)

        img_save_path = f"{run_path}/{frame_idx:04d}.png"
        images[0].save(img_save_path)
        output_frames.append(img_save_path)

        if experiment:
            experiment.log_image(img_save_path, image_name=f"{frame_idx:04d}")

    output_filename = f"{run_path}/output.gif"
    save_gif(frames=output_frames, filename=output_filename, fps=fps)

    if experiment:
        experiment.log_asset(output_filename, ftype="image")

    return output_filename


if __name__ == "__main__":
    typer.run(run)