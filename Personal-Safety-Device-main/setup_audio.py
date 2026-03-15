"""
setup_audio.py — Project Kavach
Run this ONCE to download the YAMNet model and class map into models/.
Fixed for 403 Forbidden errors.
"""

import os
import urllib.request

MODELS_DIR     = "models"
# Official YAMNet TFLite URL
MODEL_URL      = "https://storage.googleapis.com/tfhub-lite-models/google/lite-model/yamnet/classification/tflite/1.tflite"
# Official AudioSet Class Map
CLASS_MAP_URL  = "https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv"

# Updated to match the filenames expected by audio.py
MODEL_PATH     = os.path.join(MODELS_DIR, "keyword_model.tflite")
CLASS_MAP_PATH = os.path.join(MODELS_DIR, "keyword_labels.txt")

def download(url: str, dest: str, label: str) -> None:
    if os.path.exists(dest):
        size = os.path.getsize(dest)
        print(f"   [SKIP] {label} already exists ({size:,} bytes).")
        return

    print(f"   [DOWN] {label}")
    
    # --- FIXED: ADDED BROWSER HEADERS TO BYPASS 403 FORBIDDEN ---
    opener = urllib.request.build_opener()
    opener.addheaders = [('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
    urllib.request.install_opener(opener)
    # ------------------------------------------------------------

    def progress(block_count, block_size, total_size):
        if total_size > 0:
            pct = min(100, block_count * block_size * 100 // total_size)
            bar = "#" * (pct // 5) + "." * (20 - pct // 5)
            print(f"\r         [{bar}] {pct}%", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, dest, reporthook=progress)
        print()
        size = os.path.getsize(dest)
        print(f"   [OK]   Saved to {dest} ({size:,} bytes)")
    except Exception as e:
        print(f"\n   [ERR]  Failed to download {label}: {e}")

def verify_model(path: str) -> None:
    print("\n   [TEST] Verifying model loads correctly...")
    try:
        import numpy as np
        try:
            import tflite_runtime.interpreter as tflite
            interp = tflite.Interpreter(model_path=path)
        except ImportError:
            import tensorflow as tf
            interp = tf.lite.Interpreter(model_path=path)
        
        interp.allocate_tensors()
        inp = interp.get_input_details()
        out = interp.get_output_details()
        print(f"   [OK]   Model loads. Input: {inp[0]['shape']} Outputs: {out[0]['shape'][-1]}")
    except ImportError:
        print("   [WARN] Library not installed yet. Skipping verification.")
    except Exception as e:
        print(f"   [ERR]  Model failed to load: {e}")

if __name__ == "__main__":
    print("=" * 55)
    print("   Kavach Audio Setup — YAMNet Model Downloader")
    print("=" * 55)

    os.makedirs(MODELS_DIR, exist_ok=True)
    
    download(MODEL_URL, MODEL_PATH, "YAMNet TFLite model (~3.7 MB)")
    download(CLASS_MAP_URL, CLASS_MAP_PATH, "AudioSet class map CSV")

    verify_model(MODEL_PATH)

    print("\n   Next steps:")
    print("   1. Run: pip install numpy sounddevice (on your ROG)")
    print("   2. Run: python main.py")
    print("=" * 55)