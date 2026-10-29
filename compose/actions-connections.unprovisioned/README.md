Placeholder mounted at /run/prqa-connections in prqa-worker while no repository connection is provisioned.
It intentionally holds no connections.json: every `to be test` connection dispatch is rejected (CONNECTION_UNKNOWN).
Real connections live outside the project drive (default ~/.config/prqa/actions-connections, mode 750/640) and are
created only by scripts/setup/prqa_connect_repository.py. Never put a connection secret in this directory.
