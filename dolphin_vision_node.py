import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, CLIPImageProcessor
from transformers.errors import PreTrainedModelNotFoundError
from PIL import Image
from comfy.sd import CLIP
from torchvision.transforms import ToPILImage
from .utils.quantization_utils import quantize_model, unload_model, _apply_bitsandbytes_quantization
import hashlib
from typing import Tuple, Dict, Any

class DolphinVisionNode:
    """
    A ComfyUI node for generating text descriptions of images using the DolphinVision 7b model.

    This node takes an image and a text prompt as input, and generates a text description
    of the image based on the prompt using the DolphinVision 7b model. It supports
    different quantization options for the model to optimize performance and memory usage.
    """
    @classmethod
    def INPUT_TYPES(cls):
        """
        Defines the input types for the DolphinVisionNode.

        Returns:
            dict: A dictionary specifying the input types, including:
                - image (IMAGE): The input image tensor.
                - prompt (STRING): The text prompt to guide the description.
                - cache (BOOLEAN, optional): Whether to cache the quantized model (default: False).
                - quantization_type (STRING, optional): The type of quantization to apply
                  (default: "bf16 (No Quantization, Highest Quality)").
        """
        return {
            "required": {
                "image": ("IMAGE",),
                "prompt": ("STRING", {"multiline": True}),
            },
            "optional": {
                "cache": ("BOOLEAN", {"default": False}),
                "quantization_type": (
                    [
                        "bf16 (No Quantization, Highest Quality)",
                        "nf4 (Fastest, Most Efficient, Lowest Quality)",
                        "fp4 (Fast, Very Efficient, Low Quality)",
                        "int8 (Medium Speed, Efficient, Medium Quality)",
                    ],
                    {"default": "bf16 (No Quantization, Highest Quality)"}
                ),
            }
        }
        # The 'cache' parameter controls whether the quantized model is cached in memory.
        # If True, the quantized model is stored in a global cache to avoid reloading it
        # for subsequent calls with the same quantization settings.
        # This can significantly speed up the process if you are using the same
        # quantization settings repeatedly.

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("answer",)
    FUNCTION = "generate_answer"
    CATEGORY = "Divergent Nodes 👽/VLM"

    def __init__(self):
        """
        Initializes the DolphinVisionNode.

        Sets the model name, determines the device (CUDA if available, otherwise CPU),
        and initializes the model and tokenizer to None.
        """
        self.model_name = 'cognitivecomputations/dolphin-vision-7b'
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model = None
        self.tokenizer = None
        self.quantized = False  # Flag

    def load_model(self, quantization_type="bf16 (No Quantization, Highest Quality)", cache=False):
        """
        Loads the DolphinVision model and applies the specified quantization.

        Args:
            quantization_type (str): The type of quantization to apply.  Options are:
                - "bf16 (No Quantization, Highest Quality)"
                - "nf4 (Fastest, Most Efficient, Lowest Quality)"
                - "fp4 (Fast, Very Efficient, Low Quality)"
                - "int8 (Medium Speed, Efficient, Medium Quality)"
            cache (bool): Whether to cache the quantized model.

        Returns:
            Tuple[str] or None: Returns a tuple containing an error message string if an error occurs,
            otherwise returns None.
        """
        try:
            quantization_map = {
                "bf16 (No Quantization, Highest Quality)": ("bf16", None),
                "nf4 (Fastest, Most Efficient, Lowest Quality)": ("bitsandbytes", 4),
                "fp4 (Fast, Very Efficient, Low Quality)": ("bitsandbytes", 4),
                "int8 (Medium Speed, Efficient, Medium Quality)": ("bitsandbytes", 8),
            }
            quant_method, bits = quantization_map.get(quantization_type, (None, None))

            if quant_method == "bf16":
                try:
                    self.model = AutoModelForCausalLM.from_pretrained(
                        self.model_name,
                        torch_dtype=torch.bfloat16,
                        device_map="auto",
                        trust_remote_code=True
                    )
                    self.quantized = False
                except (PreTrainedModelNotFoundError, OSError) as e:
                    return (f"Error loading model '{self.model_name}'. It might not exist or there was an OSError: {e}",)
            elif quant_method == "bitsandbytes":
                self.model = quantize_model(
                    self.model_name,
                    quantization_method="bitsandbytes",
                    device=self.device,
                    cache=cache,
                    bits=bits
                )
                self.quantized = True
            else:
                return ("Invalid quantization type.",)
            if self.model is None:
                return ("Failed to load model.",)

            try:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    self.model_name,
                    trust_remote_code=True
                )
            except Exception as e:
                return (f"Error loading tokenizer: {e}",)

            self.model.eval()

        except Exception as e:
            return (f"An unexpected error occurred while loading the model: {e}",)


    def generate_answer(self, image, prompt, **kwargs):
        """
        Generates a text description of the input image based on the given prompt.

        Args:
            image (torch.Tensor): The input image tensor.  Should have shape (C, H, W) where C is 1 or 3.
            prompt (str): The text prompt to guide the description.
            **kwargs: Additional keyword arguments, including:
                - quantization_type (str, optional):  The quantization type to use.
                - cache (bool, optional): Whether to cache the quantized model.

        Returns:
            Tuple[str]: A tuple containing the generated text description (string).  Returns
            an error message if an error occurs.
        """
        self.load_model(quantization_type=kwargs.get("quantization_type", "bf16 (No Quantization, Highest Quality)"), cache=kwargs.get("cache", False))
        if self.model is None or self.tokenizer is None:
            return ("Model not loaded. Please check the node's configuration and ensure the model is loaded successfully.",)

        if not isinstance(image, torch.Tensor):
            return ("Invalid input: 'image' must be a PyTorch tensor.",)
        if not isinstance(prompt, str):
            return ("Invalid input: 'prompt' must be a string.",)

        try:
            # Convert ComfyUI image tensor to PIL Image
            pil_images = []
            for img in image:
                try:
                    if img.shape[2] not in (1, 3):  # Check for valid channel count (grayscale or RGB)
                        raise ValueError(f"Expected image tensor to have 1 or 3 channels, but got {img.shape[2]}")
                    if len(img.shape) != 3: # Check if the tensor has 3 dimensions
                        raise ValueError(f"Expected image tensor to have 3 dimensions (C, H, W), but got {len(img.shape)}")
                    pil_image = ToPILImage()(img.permute(2, 0, 1))
                    pil_images.append(pil_image)
                except ValueError as e:
                    return (f"Image processing error: {e}",)
                except Exception as e:
                    return (f"Error converting image tensor to PIL Image: {e}",)

            # Prepare text prompt
            messages = [
                {"role": "user", "content": f'<image>\n{prompt}'}
            ]
            text = self.tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True
            )

            # Prepare input
            text_chunks = [self.tokenizer(chunk).input_ids for chunk in text.split('<image>')]
            input_ids = torch.tensor(text_chunks[0] + [-200] + text_chunks[1], dtype=torch.long).unsqueeze(0)

            # Process image
            try:
                image_tensor = self.model.process_images(pil_images, self.model.config).to(dtype=self.model.dtype)
            except Exception as e:
                return(f"Error processing images with the model: {e}",)

            input_ids = input_ids.to(self.device)
            image_tensor = image_tensor.to(self.device)  # Move image_tensor to device
            # Generate output
            with torch.no_grad():
                try:
                    output_ids = self.model.generate(
                        input_ids,
                        images=image_tensor,
                        max_new_tokens=2048,
                        use_cache=True
                    )[0]
                except Exception as e:
                    return (f"Error during model generation: {e}",)

            answer = self.tokenizer.decode(output_ids[input_ids.shape[1]:], skip_special_tokens=True).strip()
            if not self.cache:
                unload_model(self.model)
                self.quantized = False
            return (answer,)

        except Exception as e:
            return (f"An unexpected error occurred: {e}",)
    
    @classmethod
    def IS_CHANGED(cls, image, prompt, **kwargs):
        """
        Detects if the inputs have changed. This method is used by ComfyUI to determine
        whether to re-execute the node.

        Args:
            image (torch.Tensor): The input image tensor.
            prompt (str): The text prompt.
            **kwargs: Additional keyword arguments (quantization_type, cache).

        Returns:
            str: A hash of the inputs (image and prompt).  Returns 0 if either image or prompt is None.
        """

        if image is None or prompt is None:
            return 0
        return hash((image.tobytes(), prompt))

    def unload(self):
        """
        Unloads the model from memory.
        """
        if self.model:
            unload_model(self.model)
