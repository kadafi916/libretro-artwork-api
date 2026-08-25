# SPDX-License-Identifier: GPL-3.0-or-later
#
# Stdlib-only app - no pip install needed, keeps the image minimal. The
# thumbnail data (many GB) is NOT baked into the image; it's mounted as a
# volume at runtime (see docker-compose.yml) so adding a new system later
# is just `git clone` into the host directory + container restart, no
# rebuild.
FROM python:3.12-slim

WORKDIR /app
COPY app.py .

ENV THUMBS_DIR=/data
ENV PORT=8090

EXPOSE 8090

CMD ["python3", "-u", "app.py"]
