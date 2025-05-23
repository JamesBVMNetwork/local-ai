#!/bin/bash
set -o pipefail

# Logging functions
log_message() {
    local message="$1"
    if [[ -n "${message// }" ]]; then
        echo "[LAUNCHER_LOGGER] [MODEL_INSTALL_LLAMA] --message \"$message\""
    fi
}

log_error() {
    local message="$1"
    if [[ -n "${message// }" ]]; then
        echo "[LAUNCHER_LOGGER] [MODEL_INSTALL_LLAMA] --error \"$message\"" >&2
    fi
}

# Error handling function
handle_error() {
    local exit_code=$1
    local error_msg=$2
    log_error "$error_msg (Exit code: $exit_code)"
    
    # Clean up if needed
    if [[ -n "$VIRTUAL_ENV" ]]; then
        log_message "Deactivating virtual environment..."
        deactivate 2>/dev/null || true
    fi
    
    exit $exit_code
}

command_exists() {
    command -v "$1" &> /dev/null
}

export PATH="/opt/homebrew/bin/:$PATH"
export PATH="$HOME/homebrew/bin:$PATH"

# Step 1: Ensure Homebrew is installed and set PATH
if ! command_exists brew; then
    log_error "Homebrew is not installed. Please install Homebrew first: https://brew.sh/"
    exit 1
fi

BREW_PREFIX=$(brew --prefix)
export PATH="$BREW_PREFIX/bin:$PATH"
log_message "Homebrew found at $BREW_PREFIX. PATH updated for this session."

# Step 2: Check system Python version and decide on installation
PYTHON_CMD="$BREW_PREFIX/bin/python3"  # Default to Homebrew Python
log_message "Checking system Python version..."
if command_exists python3; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    
    log_message "Found system Python version: $PYTHON_VERSION"
    
    # Check if version is >= 3.9
    if [ "$PYTHON_MAJOR" -gt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -ge 11 ]; }; then
        log_message "System Python is >= 3.9. Using existing Python."
        PYTHON_CMD="python3"  # Use system Python
    else
        log_message "System Python is < 3.9. Checking Homebrew Python..."
    fi
fi

# If no suitable system Python, install or verify Homebrew Python
if [ "$PYTHON_CMD" = "$BREW_PREFIX/bin/python3" ]; then
    if ! "$PYTHON_CMD" --version &>/dev/null; then
        log_message "Installing Python via Homebrew (requires >= 3.9)..."
        brew install python || handle_error $? "Failed to install Python"
    fi
    log_message "Verifying Homebrew Python version..."
    "$PYTHON_CMD" --version || handle_error $? "Python verification failed"
fi

log_message "Using Python at: $(which $PYTHON_CMD)"
log_message "Python setup complete."

# Step 3: Update PATH in .zshrc for future sessions
log_message "Checking if PATH update is needed in .zshrc..."
if ! grep -q "export PATH=\"/usr/local/bin:\$PATH\"" ~/.zshrc 2>/dev/null; then
    if [ -f ~/.zshrc ]; then
        log_message "Backing up current .zshrc..."
        cp ~/.zshrc ~/.zshrc.backup.$(date +%Y%m%d%H%M%S) || handle_error $? "Failed to backup .zshrc"
    else
        log_message "No existing .zshrc found. Skipping backup."
    fi
    
    log_message "Updating PATH in .zshrc for brew at /usr/local/bin..."
    echo "export PATH=\"/usr/local/bin:\$PATH\"" >> ~/.zshrc || handle_error $? "Failed to update .zshrc"
    log_message "Please restart your terminal or run 'source ~/.zshrc' for future sessions."
else
    log_message "PATH already updated in .zshrc."
fi

# Step 4: Install pigz
log_message "Checking for pigz installation..."
if command_exists pigz; then
    log_message "pigz is already installed."
else
    log_message "Installing pigz..."
    brew install pigz || handle_error $? "Failed to install pigz"
    log_message "pigz installed successfully."
fi

# Step 5: Install llama.cpp
log_message "Checking for llama.cpp installation..."
if command_exists llama; then
    log_message "llama.cpp is installed. Checking for updates..."
    if brew outdated | grep -q "llama.cpp"; then
        log_message "Upgrading llama.cpp..."
        brew upgrade llama.cpp || handle_error $? "Failed to upgrade llama.cpp"
        log_message "llama.cpp upgraded successfully."
    else
        log_message "llama.cpp is up to date."
    fi
else
    log_message "Installing llama.cpp..."
    brew install llama.cpp || handle_error $? "Failed to install llama.cpp"
    log_message "llama.cpp installed successfully."
fi

log_message "Verifying llama.cpp version..."
hash -r
llama-cli --version || handle_error $? "llama.cpp verification failed"
log_message "llama.cpp setup complete."


# Step 6: Create and activate virtual environment
log_message "Creating virtual environment 'local_ai'..."
"$PYTHON_CMD" -m venv local_ai || handle_error $? "Failed to create virtual environment"

log_message "Activating virtual environment..."
source local_ai/bin/activate || handle_error $? "Failed to activate virtual environment"
log_message "Virtual environment activated."


# Step 7: Install local-ai toolkit
log_message "Setting up local-ai toolkit..."
if pip show local-ai &>/dev/null; then
    log_message "local-ai is installed. Checking for updates..."
    
    # Get installed version
    INSTALLED_VERSION=$(pip show local-ai | grep Version | awk '{print $2}')
    log_message "Current version: $INSTALLED_VERSION"
    
    # Get remote version (from GitHub repository without installing)
    log_message "Checking latest version from repository..."
    TEMP_VERSION_FILE=$(mktemp)
    if curl -s https://raw.githubusercontent.com/eternalai-org/local-ai/main/local_ai/__init__.py | grep -o "__version__ = \"[0-9.]*\"" | cut -d'"' -f2 > "$TEMP_VERSION_FILE"; then
        REMOTE_VERSION=$(cat "$TEMP_VERSION_FILE")
        rm "$TEMP_VERSION_FILE"
        
        log_message "Latest version: $REMOTE_VERSION"
        
        # Compare versions
        if [ "$INSTALLED_VERSION" != "$REMOTE_VERSION" ]; then
            log_message "New version available. Updating..."
            pip uninstall local-ai -y || handle_error $? "Failed to uninstall local-ai"
            pip install -q git+https://github.com/eternalai-org/local-ai.git || handle_error $? "Failed to update local-ai toolkit"
            log_message "local-ai toolkit updated to version $REMOTE_VERSION."
        else
            log_message "Already running the latest version. No update needed."
        fi
    else
        log_message "Could not check latest version. Proceeding with update to be safe..."
        pip uninstall local-ai -y || handle_error $? "Failed to uninstall local-ai"
        pip install -q git+https://github.com/eternalai-org/local-ai.git || handle_error $? "Failed to update local-ai toolkit"
        log_message "local-ai toolkit updated."
    fi
else
    log_message "Installing local-ai toolkit..."
    pip install -q git+https://github.com/eternalai-org/local-ai.git || handle_error $? "Failed to install local-ai toolkit"
    log_message "local-ai toolkit installed."
fi

log_message "Setup completed successfully."