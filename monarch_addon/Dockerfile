ARG BUILD_FROM
FROM ${BUILD_FROM}

# HA passes the add-on version as BUILD_VERSION. We pull the app from a matching
# GitHub tag (v<version>), so the download URL changes on every version bump —
# that makes "Rebuild" cache-safe and pins the running code to a known release.
ARG BUILD_VERSION
ENV PYTHONUNBUFFERED=1

ADD https://github.com/ScopeXL/ha-monarch/archive/refs/tags/v${BUILD_VERSION}.tar.gz /tmp/app.tar.gz
RUN mkdir -p /app \
 && tar -xzf /tmp/app.tar.gz -C /app --strip-components=1 \
 && rm /tmp/app.tar.gz \
 && pip install --no-cache-dir -r /app/requirements.txt

COPY run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
