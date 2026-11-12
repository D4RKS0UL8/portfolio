# Impact Motion — Ubuntu VPS deployment

This is a static browser application. The VPS only serves the HTML, CSS, JavaScript and template files. Video preview, effects and browser-side processing run on each visitor's own device. The VPS CPU architecture (ARM64/AArch64) does not prevent the website from being served.

## Deploy on Ubuntu ARM64/AArch64

### 1. Upload the ZIP from Windows PowerShell

Download and extract nothing locally if you prefer to upload the ZIP directly. Assuming the downloaded file is in your Downloads folder:

```powershell
scp -i "C:\Users\sohei\Downloads\ssh-key-2026-08-23.key" "$env:USERPROFILE\Downloads\Impact-Motion-VPS.zip" ubuntu@132.145.50.131:/home/ubuntu/
```

### 2. SSH into the VPS

```powershell
ssh -i "C:\Users\sohei\Downloads\ssh-key-2026-08-23.key" ubuntu@132.145.50.131
```

### 3. Extract and install

Run these commands on the Ubuntu VPS:

```bash
sudo apt update
sudo apt install -y unzip
mkdir -p ~/impact-motion
unzip -o ~/Impact-Motion-VPS.zip -d ~/impact-motion
cd ~/impact-motion
sudo bash install-vps.sh
```

Then visit:

```text
http://132.145.50.131/
```

If the site does not load, check the VPS provider's network/security rules and allow inbound TCP port 80. If Ubuntu UFW is enabled, allow Nginx:

```bash
sudo ufw allow 'Nginx HTTP'
```

## Updating the website

Upload a replacement ZIP, extract it to a new folder, and run `sudo bash install-vps.sh` again. It copies the current project files into `/var/www/impact-motion`.

## Domain and HTTPS

For a public production site, point your domain's DNS A record to `132.145.50.131`, then configure HTTPS. A common option is Certbot with the Nginx plugin. Do this after DNS points to the VPS and port 80 is reachable.

## Important architecture notes

- ARM64 Ubuntu is suitable for hosting this static site with Nginx.
- Visitors' browsers execute the editor JavaScript and render the preview on their own devices; the VPS does not need a GPU for this architecture.
- The current prototype parses the supplied Alight Motion XML for transform keyframes.
- The XML references an Android `content://` image that is not part of the XML file. The visitor must import their own image.
- Current export is a browser `MediaRecorder` WebM prototype. It does not promise H.264/HEVC, NVIDIA NVENC, exact constant frame rate, or full audio export. Those require a more advanced browser encoding pipeline.
- The included installer configures HTTP only. Add HTTPS before collecting accounts or other sensitive information.
- The included editor is a prototype, not a complete CapCut replacement.
