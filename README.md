# Vidlora — video processing backend (Render / Docker)

**This is real API code, not a mockup.** It receives a permitted public YouTube, Instagram, or TikTok URL, attempts to obtain metadata using yt-dlp, and returns a temporary URL. A visit to the temporary URL makes the server attempt to download the video and deliver the file using `Content-Disposition: attachment`.

This is an **experimental developer version**. The API is deployed at https://vidlora-video-api-production.up.railway.app on a Railway trial. The health and CORS checks pass. An initial YouTube test was rejected. After the worker protocol fix, a complete 81,037,191-byte MP4 was downloaded from Railway and its video/audio decoded successfully on 2026-11-23. Platform availability can still vary; this does not guarantee every URL. Instagram and TikTok have not been tested live. Render deployment was cancelled because card verification was declined. YouTube, Instagram and TikTok may reject requests from datacenter IPs or change extraction behavior. No watermark-removal feature is provided. Do not download media without rights or authorization, and comply with source platform terms.

## Original optional Render deployment instructions

1. Unzip this backend ZIP on your computer.
2. Sign in to GitHub and create a **new repository**, e.g. `vidlora-video-api`. Use a **separate repo** from the repository where you host your APK asset.
3. Upload `app.py`, `worker.py`, `qualities.py`, `requirements.txt`, `Dockerfile`, and `render.yaml` from this ZIP to that repository root. Publish the commit.
4. In https://dashboard.render.com/ choose **New → Blueprint** and connect your new GitHub repository. Render will read `render.yaml`. Review the plan and confirm **Deploy**.
5. When the service says `Live`, open its URL, which will look like `https://vidlora-video-api.onrender.com/health` (your actual name may be different). A JSON response with `"ok": true` and `"downloads_enabled": true` confirms that the API is running.
6. **Important: a running API alone does NOT prove YouTube, Instagram or TikTok downloads work.** Test with an **owned/authorized public video**. The platforms may require authentication or reject video extraction from this hosting provider.
7. Unzip the WEBSITE ZIP separately. Open `config.js`, set `VIDEO_API_URL` to the exact Render HTTPS URL **without `/health`**, and save. For example:
   `VIDEO_API_URL: 'https://YOUR-REAL-SERVICE.onrender.com'`
8. Go to your EXISTING Netlify project https://app.netlify.com/projects/imaginative-fudge-54a13f/deploys and drag in the WEBSITE folder. Do not create a new Netlify project or change domain DNS.
9. Open https://vidlora.download/#online . Paste a public link to a video you have rights to save, check the permission checkbox, then click **Check link**. If a file is available, you will see its real title and a **Download video** button. Clicking it requests a real file attachment from the server.

### If the frontend says "Server setup needed"
You did not enter the backend's real HTTPS origin in `config.js`, or deployed the wrong site folder. No social link can work without that connection.

### If the frontend says "Could not check this link"
Open your Render URL `/health` and check Render logs. Common causes: free server is starting up, external site blocks the video, geo restrictions, anti-bot measures, or file size limitations. The page surfaces the error rather than inventing formats.

### Free plan limitations
Render free web services go to sleep when idle, take time to wake, and have CPU/RAM and outbound-bandwidth limits. Video download traffic can use that bandwidth quickly and may cause costs/limits. This starter has local-only rate limiting; add Turnstile/CAPTCHA, distributed limits, quotas and usage monitoring before opening it to large public traffic.

### Copyright / privacy
The website includes a checkbox to confirm the visitor owns or is allowed to save the video. That is not proof of rights. The website and backend use publicly accessible files only. No DRM bypass, private video access, login cookie extraction, or embedded-watermark removal. Update your website privacy policy with the real backend hosting provider, processing, retention and contact details before public use.

## API test locally (for developers)

```bash
python -m pip install -r requirements.txt
export VIDLORA_ENABLE_SOCIAL=true
export VIDLORA_PUBLIC_URL=http://localhost:8000
uvicorn app:app --host 0.0.0.0 --port 8000
```

`GET /health` -> `{ "ok": true, "downloads_enabled": true }`

`POST /api/resolve` body:

```json
{ "url": "https://www.youtube.com/watch?v=OWNED_PUBLIC_VIDEO", "source": "youtube", "authorized": true }
```

If available, the server responds with the real video title and an expiring `/api/download/...` URL. The GET endpoint returns an attachment after processing the video. Do not assume availability or quality until the live service actually succeeds.

### Deployment config

`render.yaml` sets `VIDLORA_ENABLE_SOCIAL=true`, `VIDLORA_ALLOWED_ORIGINS` to your current site, an auto-generated `VIDLORA_SIGNING_KEY`, and a max file size of 300 MiB. When using Render's assigned `.onrender.com` hostname, the service constructs its own HTTPS download links. For a custom API hostname set `VIDLORA_PUBLIC_URL=https://your-api-domain.example` in Render environment variables. Never place secrets in the website's `config.js`.

This version uses FFmpeg, Node 22 and `yt-dlp[default]` (which includes EJS support). Production-hardening and active platform tests remain necessary.



## Current deployment and limits (API 1.4)
The Railway service deploys automatically from GitHub main. Deploy app.py, worker.py, qualities.py and Dockerfile together. The existing vidlora.download frontend builds its dropdown from the API formats array, so all available quality options appear without a frontend redeploy.

The resolver groups the source's actual formats by resolution, frame rate and dynamic range. It lists each available quality, including 4K/8K only when advertised by the source. Separate original-codec and H.264/AAC iPhone MP4 profiles are offered where available. Every signed download binds the selected dimensions, frame rate, dynamic range and profile. Refreshing the source can fall back to another stream at that exact quality; it never silently reduces resolution. Original AV1/VP9/HDR files may need a compatible player on iPhone. No video is upscaled or re-encoded to invent unavailable qualities.

VIDLORA_MAX_DOWNLOAD_MB defaults to 300 MiB per finished file; the former VIDLORA_MAX_VIDEO_MB=80 setting no longer controls new downloads. VIDLORA_TEMP_STORAGE_MB defaults to 768 MiB total temporary media, including cached files and working files. Admission reserves 2.2 times the estimated output for merging; a live disk monitor enforces the total. One download prepares at a time. The current Railway free/trial service has 1 GB ephemeral storage. Oversized qualities remain visible with a capacity warning and return a clear 413 error instead of a smaller file. Multi-GB 4K files require a hosting/storage change; this code does not activate paid hosting or remove platform restrictions.

Completed files are cached for up to one hour with bounded storage. Older idle cache entries may be evicted to admit a new download; active transfers and very recent saves are protected. A restart clears the temporary cache. Links are signed and expire after one hour. Worker results use result.json separately from console output. Platform blocks, source availability and trial quotas can still prevent downloads. Instagram and TikTok require separate live validation.

## iPhone delivery
The iPhone profile preserves the chosen H.264 video and copies AAC or converts other audio to AAC-LC. MP4 output has a fast-start index. The browser preparation page requires a native tap on the final save link, avoiding large Blob objects and async automatic download clicks. HEAD and Range requests reuse the same file and ETag. Downloads use the source title in a sanitized UTF-8 filename. This validates the server's iPhone delivery behavior; physical-device saving must still be checked on an iPhone.
