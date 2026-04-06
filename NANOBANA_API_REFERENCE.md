# Nanobana API Reference - Complete Format & Examples

**Status: Generated from codebase analysis**
**Last Updated: 2026-04-06**

---

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `https://api.nanobananaapi.ai/api/v1/nanobanana/generate` | POST | Submit image enhancement task, get `taskId` |
| `https://api.nanobananaapi.ai/api/v1/nanobanana/record-info` | GET | Query task status by `taskId` |

---

## Authentication

All requests require Bearer token authorization:

```python
headers = {
    "Authorization": f"Bearer {NANOBANA_API_KEY}",
    "Content-Type": "application/json"
}
```

API Key location in `.env`:
```
NANOBANA_API_KEY="366060ed2f3a25e5640ed861e43a15ac"
```

---

## POST /generate Request

### ⚠️ CRITICAL: Two Versions Exist in Codebase

#### **Version 1 (CURRENT - app/services/ai.py) - MINIMAL PAYLOAD:**
```python
payload = {
    "prompt": str,           # Required: Text instruction for image enhancement
    "type": "imagetoimage",  # Required: Use lowercase "imagetoimage"
    "imageUrls": [str],      # Required: Array with one public image URL
}
```

**Example:**
```json
{
    "prompt": "Place the jewelry on a dark navy-blue sculpted stone surface. Soft directional studio lighting from the upper-left...",
    "type": "imagetoimage",
    "imageUrls": ["https://storage.supabase.co/...product.jpg"]
}
```

#### **Version 2 (LEGACY - ai_clients.py) - WITH IMAGE_SIZE:**
```python
payload = {
    "prompt": str,              # Required
    "type": "IMAGETOIMAGE",     # Note: UPPERCASE - causes issues
    "imageUrls": [str],         # Required
    "image_size": "1:1"         # ⚠️ API rejects this: 'msg': 'Incorrect type'
}
```

### Known Rejected Parameters

These parameters were attempted but rejected by the API:

| Parameter | Values Tried | API Error | Status |
|-----------|--------------|-----------|--------|
| `image_size` | `"1:1"` | `'msg': 'Incorrect type'` | ❌ REJECTED |
| `resolution` | `"1K"`, `"1k"` | `'msg': 'The image resolution is wrong'` | ❌ REJECTED |
| `type` | `"IMAGETOIMAGE"` (uppercase) | `'msg': 'Incorrect type'` | ❌ REJECTED |

### Correct Flag

```
type MUST be lowercase: "imagetoimage"
```

---

## POST /generate Response

### Success Response (HTTP 200)
```json
{
    "taskId": "abc123xyz789",
    "data": {
        "taskId": "abc123xyz789"
    }
    // May also include additional metadata
}
```

### Fields to Extract
- Look for `taskId` at: `response["taskId"]` OR `response["data"]["taskId"]` OR `response["data"]["id"]`

### Error Response (HTTP 4xx/5xx)
```json
{
    "msg": "The image resolution is wrong"
}
// OR
{
    "msg": "Incorrect type"
}
// OR other error messages
```

---

## GET /record-info Status Polling

### Request Format
```
GET https://api.nanobananaapi.ai/api/v1/nanobanana/record-info?taskId=abc123xyz789
Headers:
    Authorization: Bearer {NANOBANA_API_KEY}
    Content-Type: application/json
```

### Polling Strategy
- **Interval:** 5 seconds between polls
- **Max Polls:** 60 (total timeout: 300 seconds = 5 minutes)
- **Typical Time:** 10-40 seconds for completion

### Success Response (Task Complete)
```json
{
    "successFlag": 1,  // Can be int 1 or string "1"
    "data": {
        "successFlag": 1,
        "response": {
            "resultImageUrl": "https://cdn.nanobanana.ai/result-xyz.jpg"
        },
        "resultImageUrl": "https://cdn.nanobanana.ai/result-xyz.jpg",
        "result_image_url": "https://cdn.nanobanana.ai/result-xyz.jpg",
        "imageUrl": "https://cdn.nanobanana.ai/result-xyz.jpg",
        "image_url": "https://cdn.nanobanana.ai/result-xyz.jpg"
    }
}
```

### Processing Response (Task Still Running)
```json
{
    "data": {
        "successFlag": 0,
        "failFlag": 0
    }
}
```

### Failure Response (Task Failed)
```json
{
    "failFlag": 1,  // Can be int 1 or string "1"
    "data": {
        "failFlag": 1,
        "error": "Error message"
    }
}
```

### Response Parsing Logic
```python
data = status_data.get("data") or {}

# Check for success - can be in multiple locations
success = (
    data.get("successFlag") in (1, "1")
    or status_data.get("successFlag") in (1, "1")
)

if success:
    # Result URL can be in ANY of these fields (API version compatibility):
    result_url = (
        (data.get("response") or {}).get("resultImageUrl")
        or data.get("resultImageUrl")
        or data.get("result_image_url")
        or data.get("imageUrl")
        or data.get("image_url")
        or status_data.get("resultImageUrl")
        or status_data.get("imageUrl")
    )

# Check for failure
fail_flag = data.get("failFlag") or status_data.get("failFlag")
if fail_flag in (1, "1"):
    # Task failed
```

---

## Example: Complete End-to-End Flow

### Step 1: Submit Task
```python
import httpx
import asyncio

async def enhance_image(image_url: str):
    headers = {
        "Authorization": f"Bearer {NANOBANA_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "prompt": "Place the jewelry on dark navy-blue stone with soft studio lighting...",
        "type": "imagetoimage",
        "imageUrls": [image_url]
    }
    
    # Submit
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.nanobananaapi.ai/api/v1/nanobanana/generate",
            headers=headers,
            json=payload
        )
        task_data = response.json()
        task_id = task_data.get("taskId") or task_data.get("data", {}).get("taskId")
        print(f"Task submitted: {task_id}")
```

### Step 2: Poll for Status
```python
    # Poll
    max_polls = 60
    poll_interval = 5
    
    for i in range(max_polls):
        await asyncio.sleep(poll_interval)
        
        async with httpx.AsyncClient(timeout=30) as client:
            status_response = await client.get(
                f"https://api.nanobananaapi.ai/api/v1/nanobanana/record-info?taskId={task_id}",
                headers=headers
            )
            status_data = status_response.json()
        
        data = status_data.get("data") or {}
        success = data.get("successFlag") in (1, "1") or status_data.get("successFlag") in (1, "1")
        
        if success:
            # Step 3: Extract result URL
            result_url = (
                (data.get("response") or {}).get("resultImageUrl")
                or data.get("resultImageUrl")
                or status_data.get("resultImageUrl")
            )
            print(f"Task complete! Result: {result_url}")
            
            # Download result
            async with httpx.AsyncClient(timeout=120) as client:
                img_response = await client.get(result_url, follow_redirects=True)
                img_bytes = img_response.content
                print(f"Downloaded: {len(img_bytes)} bytes")
                return img_bytes
        
        fail_flag = data.get("failFlag") or status_data.get("failFlag")
        if fail_flag in (1, "1"):
            raise RuntimeError(f"Task failed: {status_data}")
        
        if i % 5 == 0:
            print(f"Poll {i+1}/{max_polls}... still waiting")
    
    raise TimeoutError(f"Task {task_id} took too long (>5 minutes)")
```

---

## Curl Examples

### Submit Task
```bash
curl -X POST https://api.nanobananaapi.ai/api/v1/nanobanana/generate \
  -H "Authorization: Bearer 366060ed2f3a25e5640ed861e43a15ac" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "Place the jewelry on a dark navy-blue sculpted stone surface with soft studio lighting",
    "type": "imagetoimage",
    "imageUrls": ["https://storage.supabase.co/...image.jpg"]
  }'
```

### Poll Status
```bash
curl -X GET "https://api.nanobananaapi.ai/api/v1/nanobanana/record-info?taskId=abc123xyz789" \
  -H "Authorization: Bearer 366060ed2f3a25e5640ed861e43a15ac"
```

---

## Implementation Files

### Primary Implementation
- **[app/services/ai.py](app/services/ai.py)** — Current preferred implementation (minimal payload)
  - Class: `NanobanaClient`
  - Method: `enhance_image(image_url, prompt=None)`
  - Uses simplified payload (no `image_size` or `resolution` fields)

### Legacy Implementation
- **[ai_clients.py](ai_clients.py)** — Original implementation (with `image_size` field)
  - Class: `NanobanaClient`
  - Method: `enhance_image(image_url, prompt=None)`
  - Contains commented debugging and raw response logging

### Configuration
- **[app/config.py](app/config.py)** — Settings and variant prompts
  - `NANOBANA_API_KEY` — Bearer token
  - `NANOBANA_PROMPT` — Default prompt (can be overridden per-call)
  - `NANOBANA_VARIANT_PROMPT_1` through `_4` — The 4 style variants

### Pipeline Orchestration
- **[app/services/pipeline.py](app/services/pipeline.py)** — Calls `NanobanaClient.enhance_image()` 4 times in parallel for variants

---

## Default Prompt Template

From `.env` file:
```
NANOBANA_PROMPT="Use the provided reference image as the exact background and scene composition. Replace only the jewellery with the new jewellery image provided. Place the jewellery naturally on the same curved stone surface, maintaining the same position, scale, and perspective as the reference. Match studio lighting precisely — soft directional light with subtle highlights, controlled reflections, and realistic depth shadows where the jewellery touches the stone. Preserve all fine details of the jewellery exactly as in the original image: metal texture, gemstone cuts, color accuracy, brilliance, engravings, and proportions must remain unchanged..."
```

---

## Variant Prompts (4 Image Styles)

The pipeline generates 4 professional variants:

1. **Variant 1 - Dark Stone (Navy Blue)**
   - Surface: Dark navy-blue sculpted stone
   - Angle: Front-facing, centered
   - Lighting: Upper-left directional
   - Mood: Deep, moody, luxurious

2. **Variant 2 - Velvet (Burgundy Red)**
   - Surface: Deep burgundy-red velvet cushion
   - Angle: 45-degree front-left perspective
   - Lighting: Upper-right warm golden
   - Mood: Boutique aesthetic, shallow depth of field

3. **Variant 3 - Marble (White)**
   - (See [app/config.py](app/config.py) lines 30-50)

4. **Variant 4 - Charcoal (Dark)**
   - (See [app/config.py](app/config.py) lines 50-70)

---

## Troubleshooting

### ❌ "The image resolution is wrong"
- **Cause:** Using `"resolution": "1K"` or similar parameter
- **Fix:** Remove the `resolution` parameter entirely. API handles it automatically.

### ❌ "Incorrect type"
- **Cause:** Either `"type": "IMAGETOIMAGE"` (uppercase) OR `"image_size": "1:1"` in payload
- **Fix:** Use `"type": "imagetoimage"` (lowercase) only. Remove `image_size`.

### ⏱️ Task takes too long (>300 seconds)
- **Cause:** API is overloaded or image too complex
- **Fix:** Decrease max_polls to 30 (150 seconds) for faster timeout, or increase to 120 (600 seconds) for more patient polling

### 🎨 Result image is wrong size/quality
- **Cause:** Cannot control via API parameters (API rejects size/resolution params)
- **Fix:** API decides output size. Post-process in application if needed.

### ❌ Task fails silently
- **Cause:** API returns `"failFlag": 1` without error message
- **Log:** Check status_data from polling response for error clues
- **Debug:** Log full response at failed status check

---

## Notes on the Issue

### Credits Charging Issue (Original Bug)
- User was charged 18 credits (4K resolution) instead of 1K
- **Root Cause:** Previous code included `"image_size": "1:1"` and/or `"type": "IMAGETOIMAGE"` which may have been incorrectly triggering higher resolution
- **Fix:** Removed both fields, using minimal payload with only `prompt`, `type: "imagetoimage"`, and `imageUrls`
- **Current Status:** Minimal payload in [app/services/ai.py](app/services/ai.py)

---

## Files with API Information

| File | Contains |
|------|----------|
| [app/services/ai.py](app/services/ai.py) | Current client implementation + full polling logic |
| [ai_clients.py](ai_clients.py) | Legacy implementation with detailed comments |
| [app/config.py](app/config.py) | API key + variant prompts + default prompt |
| [.env](.env) | API credentials + prompt examples |
| [PIPELINE_DESIGN.md](PIPELINE_DESIGN.md) | High-level flow explanation (lines 350-400) |
| [app/README.md](app/README.md) | Integration guide + component breakdown |

---

## Summary

✅ **Correct Payload:**
```json
{
    "prompt": "...",
    "type": "imagetoimage",
    "imageUrls": ["https://..."]
}
```

❌ **Incorrect Payloads to Avoid:**
```json
// NO: uppercase type
{"prompt": "...", "type": "IMAGETOIMAGE", "imageUrls": [...]}

// NO: image_size parameter
{"prompt": "...", "type": "imagetoimage", "imageUrls": [...], "image_size": "1:1"}

// NO: resolution parameter
{"prompt": "...", "type": "imagetoimage", "imageUrls": [...], "resolution": "1K"}
```
