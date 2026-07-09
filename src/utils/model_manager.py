import gc
import logging
import os
from typing import Any

try:
    import torch
except ImportError:
    torch = None  # type: ignore[assignment]

logger = logging.getLogger("captcha_solver")


class ModelManager:
    """
    Dependency Injection container and Context Manager for heavy ML models.
    Lazy-loads models only when requested, and can unload them to free VRAM/RAM.
    """

    def __init__(self, whisper_model_size: str = "base.en", clip_model_name: str = "ViT-B-32", ocr_languages: list[str] | None = None, gpu: bool = True):
        self.whisper_model_size = whisper_model_size
        self.clip_model_name = clip_model_name
        self.ocr_languages = ocr_languages or ["en"]
        self.gpu = gpu

        if torch is not None:
            self._device = "cuda" if (gpu and torch.cuda.is_available()) else "cpu"
        else:
            self._device = "cpu"

        self._whisper_model = None
        self._clip_model = None
        self._clip_preprocess = None
        self._clip_tokenizer = None
        self._clip_device = self._device
        self._easyocr_reader = None
        self._ddddocr_reader = None

    def _get_model_path(self) -> str:
        model_dir = os.path.join(os.path.dirname(__file__), "..", "..", "models")
        return os.path.abspath(model_dir)

    def get_whisper(self) -> Any:
        if self._whisper_model is None:
            try:
                from faster_whisper import WhisperModel
                compute_type = "float16" if self._device == "cuda" else "int8"
                logger.info(f"loading faster-whisper model '{self.whisper_model_size}' on {self._device}...")
                self._whisper_model = WhisperModel(
                    self.whisper_model_size,
                    device=self._device,
                    compute_type=compute_type,
                    download_root=self._get_model_path(),
                )
                logger.info("faster-whisper ready")
            except ImportError:
                logger.error("faster-whisper not installed. Run: pip install faster-whisper")
                raise
        return self._whisper_model

    def get_clip(self) -> tuple[Any, Any, Any, str]:
        if self._clip_model is None:
            try:
                import open_clip

                logger.info(f"loading CLIP model '{self.clip_model_name}' on {self._device}...")
                self._clip_model, _, self._clip_preprocess = open_clip.create_model_and_transforms(
                    self.clip_model_name, pretrained="laion2b_s34b_b79k"
                )
                self._clip_tokenizer = open_clip.get_tokenizer(self.clip_model_name)
                self._clip_model = self._clip_model.to(self._device)
                self._clip_device = self._device
                self._clip_model.eval()
                logger.info("CLIP ready")
            except ImportError:
                logger.error("open-clip-torch not installed. Run: pip install open-clip-torch")
                raise
        return self._clip_model, self._clip_preprocess, self._clip_tokenizer, self._clip_device

    def get_easyocr(self) -> Any:
        if self._easyocr_reader is None:
            try:
                import easyocr
                use_gpu = self._device == "cuda"
                logger.info(f"loading EasyOCR (first use, gpu={use_gpu})...")
                self._easyocr_reader = easyocr.Reader(self.ocr_languages, gpu=use_gpu)
                logger.info("EasyOCR ready")
            except ImportError:
                logger.error("easyocr not installed. Run: pip install easyocr")
                raise
        return self._easyocr_reader

    def get_ddddocr(self) -> Any:
        """Lazy-load ddddocr — an OCR model specialized for distorted-text captchas.
        Returns the reader, or False (sentinel) if the package isn't installed, so
        callers can gracefully skip it without the whole pipeline failing."""
        if self._ddddocr_reader is None:
            try:
                import ddddocr
                logger.info("loading ddddocr (captcha-specialized OCR)...")
                self._ddddocr_reader = ddddocr.DdddOcr(show_ad=False)
                logger.info("ddddocr ready")
            except Exception as e:
                logger.warning(f"ddddocr unavailable ({e}) — skipping. Run: pip install ddddocr")
                self._ddddocr_reader = False
        return self._ddddocr_reader

    def unload_all(self):
        """Unloads all models and frees memory."""
        unloaded = False
        if self._whisper_model is not None:
            self._whisper_model = None
            unloaded = True

        if self._clip_model is not None:
            self._clip_model = None
            self._clip_preprocess = None
            self._clip_tokenizer = None
            unloaded = True

        if self._easyocr_reader is not None:
            self._easyocr_reader = None
            unloaded = True

        if self._ddddocr_reader not in (None, False):
            self._ddddocr_reader = None
            unloaded = True

        if unloaded:
            logger.info("Unloaded ML models from memory")
            gc.collect()
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.unload_all()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.unload_all()
