# Local ai Toolkit

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Deploy and manage large language models locally with minimal setup.

## 📋 Table of Contents

- [Overview](#overview)
- [Installation](#installation)
- [Usage](#usage)
- [Contributing](#contributing)

## 🔭 Overview

The **Local ai Toolkit** empowers developers to deploy state-of-the-art large language models directly on their machines. Bypass cloud services, maintain privacy, and reduce costs while leveraging powerful AI capabilities.

## ✨ Features

- **Simple Deployment**: Get models running with minimal configuration
- **Filecoin Integration**: Upload and download models directly from decentralized storage (Filecoin)

## 📦 Installation

### MacOS

```bash
bash mac.sh
```

### Verification

Confirm successful installation:

```bash
source local_ai/bin/activate
local-ai --version
```

## 🚀 Usage

### Managing Models

```bash
# Check if model is available locally
local-ai check --hash <filecoin_hash>

# Upload a model to Filecoin
local-ai upload --folder-name <folder_name> --task <task>

# Download a model from Filecoin
local-ai download --hash <filecoin_hash>

# Start a model
local-ai start --hash <filecoin_hash>

# Example
local-ai start --hash bafkreiecx5ojce2tceibd74e2koniii3iweavknfnjdfqs6ows2ikoow6m

# Check running models
local-ai status

# Stop the current model
local-ai stop

```
### Important Notes on Uploading Models

When using the `upload` command, the following flags are required:

- **`--folder-name`**: Specifies the directory containing your model. The model file must have the same name as this folder.

**Example Upload Command:**

```bash

# Upload a BERT model for text classification
local-ai upload --folder-name bert-classifier
```

Make sure your folder structure follows this convention:
```
llama2-7b/
├── llama2-7b (the model file with `gguf` format)
```


## 👥 Contributing

Contributions welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for details.