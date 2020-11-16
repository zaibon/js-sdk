from jumpscale.loader import j
from jumpscale.servers.gedis.baseactor import BaseActor, actor_method

from shlex import quote
from kubernetes import client, config


class HelmChart(BaseActor):
    def __init__(self):
        super().__init__()
        k8s_client = j.clients.kubernetes.get("marketplace_vdc")

    @actor_method
    def install_helm_chart(
        self, repo_name: str, repo_url: str, release: str, chart_name: str, config: dict = None
    ) -> str:
        config = config or {}

        k8s_client.add_helm_repo(repo_name, repo_url)
        k8s_client.update_helm_repo()
        k8s_client.install_chart(release=release, chart_name=chart_name, config=config)

        return j.data.serializers.json.dumps("Chart installed")

    @actor_method
    def uninstall_helm_chart(self, release: str) -> str:
        k8s_client.delete_deployed_release(release)
        return j.data.serializers.json.dumps("Chart uninstalled")


Actor = HelmChart
