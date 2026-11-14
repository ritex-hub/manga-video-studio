import io
import base64
import tempfile
import modal

app = modal.App("manga-studio-animator")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0")
    .pip_install(
        "torch==2.4.0",
        "torchvision==0.19.0",
        "diffusers==0.30.3",
        "transformers==4.44.2",
        "accelerate==0.33.0",
        "imageio[ffmpeg]",
        "pillow",
        "fastapi[standard]"
    )
)

model_volume = modal.Volume.from_name("svd-model-cache", create_if_missing=True)

@app.cls(
    image=image,
    gpu="L4",
    volumes={"/cache": model_volume},
    scaledown_window=60,
    timeout=300
)
class MangaAnimator:
    @modal.enter()
    def load_model(self):
        import torch
        from diffusers import StableVideoDiffusionPipeline

        self.pipe = StableVideoDiffusionPipeline.from_pretrained(
            "stabilityai/stable-video-diffusion-img2vid-xt",
            torch_dtype=torch.float16,
            variant="fp16",
            cache_dir="/cache"
        )
        self.pipe.to("cuda")
        self.pipe.enable_model_cpu_offload()

    @modal.fastapi_endpoint(method="POST")
    def generate(self, payload: dict):
        import torch
        from PIL import Image
        from diffusers.utils import export_to_video

        img_b64 = payload.get("image_base64", "")
        if not img_b64:
            return {"error": "Falta image_base64"}

        raw_bytes = base64.b64decode(img_b64)
        init_image = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
        init_image = init_image.resize((576, 1024), Image.Resampling.LANCZOS)

        generator = torch.manual_seed(42)
        frames = self.pipe(
            init_image,
            decode_chunk_size=8,
            generator=generator,
            motion_bucket_id=140,
            noise_aug_strength=0.04,
            num_frames=25
        ).frames[0]

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_vid:
            out_path = tmp_vid.name

        export_to_video(frames, out_path, fps=8)

        with open(out_path, "rb") as vf:
            vid_b64 = base64.b64encode(vf.read()).decode("utf-8")

        return {"video_base64": vid_b64, "gpu": "Modal L4 SVD-XT"}
