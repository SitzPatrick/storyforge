# StoryForge Unraid Web Controller

This milestone adds a lightweight single-user web interface for StoryForge.
It runs in one Docker container, stores persistent data only in mapped volumes, and reuses the existing StoryForge CLI and application modules.

## 1. Build the image

From the repository root:

```bash
docker build -t storyforge:latest .
```

The image includes:

- StoryForge
- FastAPI
- Uvicorn
- Jinja2 templates
- multipart upload support
- FFmpeg

## 2. Deploy with Docker Compose

Use `docker-compose.unraid.yml`:

```bash
docker compose -f docker-compose.unraid.yml up -d --build
```

The web UI listens on port `8787`.

## 3. Install with the Unraid XML template

Import `unraid/storyforge.xml` into Unraid Community Applications as a custom template.

It exposes fields for:

- Web UI port
- `/config`
- `/data/books`
- `/data/projects`
- `/data/cache`
- `/data/output`
- `/data/logs`
- `PUID`
- `PGID`
- `UMASK`
- `TZ`
- `KOKORO_URL`

## 4. Required path mappings

Map the container paths exactly as follows:

- `/config` → `/mnt/user/appdata/storyforge/config`
- `/data/books` → `/mnt/user/storyforge/books`
- `/data/projects` → `/mnt/user/storyforge/projects`
- `/data/cache` → `/mnt/user/storyforge/cache`
- `/data/output` → `/mnt/user/storyforge/output`
- `/data/logs` → `/mnt/user/appdata/storyforge/logs`

All persistent project metadata, logs, and artifacts stay under these mappings.

## 5. Port configuration

The web UI defaults to port `8787`.

Open:

```text
http://UNRAID-IP:8787
```

If you change the port, update the Docker publish mapping and the StoryForge port environment variable together.

## 6. Kokoro connection setup

By default the container points at:

```text
http://kokoro:8880
```

If Kokoro runs in another container, place both containers on the same Docker network and set `KOKORO_URL` accordingly.

The web controller does not need Kokoro just to load the dashboard.
It only needs Kokoro when you run StoryForge diagnostics or start build/analyze work that calls the existing StoryForge engine.

## 7. First startup

1. Start the container.
2. Open `http://UNRAID-IP:8787`.
3. Create a project.
4. Upload an EPUB or choose one from `/data/books`.
5. Open the project page and run analysis or a build.

The dashboard shows all existing projects and their latest state.

## 8. Uploading a book

On the Create Project page, you can either:

- upload an EPUB from your browser, or
- select an EPUB already stored in `/data/books`

Uploads are validated for:

- `.epub` extension
- secure filenames
- maximum upload size
- no directory traversal

## 9. Starting a build

Open a project and choose **Build**.

The web controller starts a background StoryForge subprocess and returns immediately.
Only one active build can run at a time.

The build page shows:

- current status
- stage
- chapter progress when available
- last message
- start and finish time
- live log output

## 10. Viewing logs

Open the build page to see the current log.
The page polls status and log updates every two seconds.

The full build log is also stored at:

```text
/data/projects/<project-slug>/build.log
```

## 11. Downloading an M4B

Open the Artifacts page for a project and download generated files from there.

The controller only serves files inside the project artifacts tree.
It does not allow arbitrary filesystem downloads.

## 12. Cancelling and resuming

If a build is running, click **Cancel**.
The controller will:

1. mark the job cancelling
2. terminate the subprocess gracefully
3. wait briefly
4. force termination if required
5. preserve completed artifacts and logs

To continue later, click **Resume** or **Build** again.
The underlying StoryForge pipeline remains incremental, so completed work can be reused.

## 13. Updating the container

1. Pull or rebuild the image.
2. Stop the old container.
3. Start the new one with the same volume mappings.

The important state lives in `/config` and `/data/projects`.

## 14. Backing up `/config` and `/data/projects`

Back up these directories regularly:

- `/config`
- `/data/projects`

Those contain the web controller settings, project metadata, logs, and artifacts.

## 15. Common permission problems

If StoryForge cannot write to mapped paths:

- check `PUID`
- check `PGID`
- check `UMASK`
- verify the host directories are writable by the mapped user

The container drops privileges after startup when possible.

## 16. LAN-only security guidance

This controller is designed for trusted home LAN use.
It does not add accounts or authentication.

Recommended practice:

- expose it only on your LAN
- do not publish it directly to the internet
- keep it on a private Docker network if Kokoro is containerized
- avoid mounting extra host paths beyond the required mappings

## CLI usage

The same implementation is also available from the CLI:

```bash
storyforge web --host 0.0.0.0 --port 8787
```
