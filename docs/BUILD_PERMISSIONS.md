# Build-context permissions

Production failed to import /app/backend/bonuspro/api/app.py with PermissionError after a Git worktree checkout under umask 077. Earlier bridge.js requests returned a Content-Length but zero body bytes. Source files checked out with 0600 can be copied into Docker images as root-owned 0600; runtime UID 10001 then cannot read them. Host file ownership is not a reliable image ownership policy.

The Dockerfile normalizes only public application directories backend, web, migrations and deploy with u=rwX,go=rX, and public runtime configuration files to 0644, before USER 10001:10001. Directories remain traversable, existing executable files stay executable, normal files become readable, and runtime users receive no write permission. The application container remains read-only and non-root.

.dockerignore excludes deploy/secrets; actual secrets are mounted under /run/secrets and their modes and ownership are unchanged. No chmod targets /run/secrets, database volumes or host backups.

Container CI explicitly converts checkout directories to 0700 and ordinary source files to 0600 before the Docker build. The same non-root image must then bootstrap database roles, migrate, start healthy and serve every static asset byte-for-byte through HTTP/1.1 and HTTP/2 with compression enabled and disabled. This reproduces the missing permission scenario rather than relying on the runner's default umask.
