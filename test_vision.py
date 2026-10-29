import base64
import mimetypes
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")


def main():
    if len(sys.argv) < 3:
        print(
            'Usage: python test_vision.py test-screen.png "File menu"'
        )
        sys.exit(1)

    image_path = Path(sys.argv[1]).expanduser()
    target = " ".join(sys.argv[2:]).strip()

    if not image_path.is_file():
        print(f"Screenshot not found: {image_path}")
        sys.exit(1)

    api_key = os.getenv("BASETEN_API_KEY")
    if not api_key:
        print("BASETEN_API_KEY is missing from your .env file.")
        sys.exit(1)

    mime_type, _ = mimetypes.guess_type(image_path.name)
    if mime_type not in ("image/png", "image/jpeg", "image/webp"):
        print("Use a PNG, JPEG, or WebP screenshot.")
        sys.exit(1)

    image_bytes = image_path.read_bytes()
    if len(image_bytes) > 30 * 1024 * 1024:
        print("Image is over 30 MB. Use a smaller PNG or JPEG file.")
        sys.exit(1)

    print(
        f"Screenshot: {image_path.name} "
        f"({len(image_bytes) / 1024:.1f} KB)"
    )
    print(f"Target: {target}")
    print("Sending one image request to Baseten...")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    image_url = f"data:{mime_type};base64,{encoded}"

    client = OpenAI(
        api_key=api_key,
        base_url="https://inference.baseten.co/v1",
        timeout=120.0,
        max_retries=1,
    )

    prompt = f"""
You are the visual grounding component of OmniGuide,
an AI desktop companion.

Locate this exact visible UI element in the supplied screenshot:
"{target}"

Return a JSON object with these fields:
- target: the requested element
- found: true or false
- bbox_1000: [x_min, y_min, x_max, y_max], or null
- reason: a short explanation of the visual evidence

Coordinates must be integers from 0 to 1000 relative to the
complete supplied image. The origin (0,0) is top-left and
(1000,1000) is bottom-right.

Make the bounding box as tight as possible around the target.
Do not assume the target's position based on common UI layouts.
Do not guess. If the target is not visibly identifiable,
set found to false and bbox_1000 to null.

Return only valid JSON. Do not include Markdown fences.
"""

    try:
        response = client.chat.completions.create(
            model="meta-models/muse-spark-open-1026",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url},
                        },
                    ],
                }
            ],
        )

        print("\n--- Muse Spark Open response ---\n")
        print(response.choices[0].message.content)

        if response.usage:
            print("\n--- Token usage ---")
            print(response.usage)

    except Exception as exc:
        print(f"\nRequest failed: {type(exc).__name__}: {exc}")
        print(
            "\nCheck your Baseten key, credit balance, model access, "
            "and the endpoint's error message."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
