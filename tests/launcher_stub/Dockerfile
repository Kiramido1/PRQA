# DISPOSABLE stub image for tests/test_launcher_integration.py: the real entrypoint, verifier and extractor from
# worker/sandbox/ (copied into the build context by the test) plus a stub `opencode` that writes a synthetic result.
# Built offline from the locally cached python:3.13-slim with --network none. Never used outside the test.
FROM python:3.13-slim
RUN groupadd -g 10001 sandbox && useradd -u 10001 -g 10001 -M -d /home/sandbox -s /bin/bash sandbox
COPY entrypoint.sh /usr/local/bin/prqa-sandbox-entrypoint
COPY verify_checkout.py /usr/local/bin/prqa-verify-checkout
COPY populate_checkout.py /usr/local/bin/prqa-populate-checkout
COPY stub_opencode.py /usr/local/bin/opencode
COPY result-template.json /usr/local/share/prqa-stub-result.json
RUN chmod 0555 /usr/local/bin/prqa-sandbox-entrypoint /usr/local/bin/prqa-verify-checkout /usr/local/bin/prqa-populate-checkout /usr/local/bin/opencode \
    && chmod 0444 /usr/local/share/prqa-stub-result.json && mkdir -p /checkout && chown 10001:10001 /checkout && chmod 0755 /checkout
USER 10001:10001
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/prqa-sandbox-entrypoint"]
