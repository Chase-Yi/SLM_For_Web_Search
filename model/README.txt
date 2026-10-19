=====================================================================
 model/  --  Put your model GGUF file in this folder
=====================================================================

This project runs a local small language model, and that model is a
single GGUF file. Download it and save it in THIS folder, with exactly
this name:

    model/Qwen3.5-0.8B-BF16.gguf

Download link (Hugging Face, about 1.5 GB):

    https://huggingface.co/unsloth/Qwen3.5-0.8B-GGUF/resolve/main/Qwen3.5-0.8B-BF16.gguf?download=true

Important notes:

- The file name and folder must match exactly. Every script in this
  project loads the model from "model/Qwen3.5-0.8B-BF16.gguf"
  (relative to the project root), so a renamed or misplaced file will
  crash with a "file not found" error.

- Do not commit your GGUF file to git. Model files are large, so
  .gitignore keeps everything in this folder ignored -- except this
  README.txt, which is the only tracked file here. After a fresh clone
  this folder contains nothing but this text file, waiting for your
  download.

- Full step-by-step setup instructions: see docs/00_environment_setup.md
  (Step 3 -- download the model file).

=====================================================================
