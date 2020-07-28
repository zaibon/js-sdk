from jumpscale.loader import j
from jumpscale.sals.chatflows.chatflows import GedisChatBot, StopChatFlow, chatflow_step
from jumpscale.sals.reservation_chatflow.models import SolutionType
from jumpscale.sals.reservation_chatflow import deployer, solutions
import uuid


class kubernetesDeploy(GedisChatBot):
    steps = [
        "deployment_start",
        "kubernetes_name",
        "choose_flavor",
        "nodes_selection",
        "network_selection",
        "public_key_get",
        "ip_selection",
        "overview",
        "reservation",
        "success",
    ]
    title = "Kubernetes"

    @chatflow_step()
    def deployment_start(self):
        self.solution_id = uuid.uuid4().hex
        self.md_show("# This wizard will help you deploy a kubernetes cluster")

    @chatflow_step(title="Solution name")
    def kubernetes_name(self):
        valid = False
        while not valid:
            self.solution_name = deployer.ask_name(self)
            k8s_solutions = solutions.list_kubernetes_solutions(sync=False)
            valid = True
            for sol in k8s_solutions:
                if sol["Name"] == self.solution_name:
                    valid = False
                    self.md_show("The specified solution name already exists. please choose another.")
                    break
                valid = True

    @chatflow_step(title="Master and Worker flavors")
    def choose_flavor(self):
        form = self.new_form()
        sizes = ["1 vCPU 2 GiB ram 50GiB disk space", "2 vCPUs 4 GiB ram 100GiB disk space"]
        cluster_size_string = form.drop_down_choice("Choose the size of your nodes", sizes, default=sizes[0])

        self.workernodes = form.int_ask(
            "Please specify the number of worker nodes", default=1, required=True, min=1
        )  # minimum should be 1

        form.ask()
        self.cluster_size = sizes.index(cluster_size_string.value) + 1
        if self.cluster_size == 1:
            self.master_query = self.worker_query = {"sru": 50, "mru": 2, "cru": 1}
        else:
            self.master_query = self.worker_query = {"sru": 100, "mru": 4, "cru": 2}

    @chatflow_step(title="Containers' node id")
    def nodes_selection(self):
        queries = [self.master_query] * (self.workernodes.value + 1)
        workload_names = ["Master node"] + [f"Slave node {i+1}" for i in range(self.workernodes.value)]
        self.selected_nodes, self.selected_pool_ids = deployer.ask_multi_pool_placement(
            self, len(queries), queries, workload_names=workload_names
        )

    @chatflow_step(title="Network")
    def network_selection(self):
        self.network_view = deployer.select_network(self)

    @chatflow_step(title="Access keys and secret")
    def public_key_get(self):
        self.ssh_keys = self.upload_file(
            """Please add your public ssh key, this will allow you to access the deployed containers using ssh.
                Just upload the file with the key.
                Note: please use keys compatible with Dropbear server eg: rsa """,
            required=True,
        ).split("\n")

        self.cluster_secret = self.string_ask("Please add the cluster secret", default="secret")

    @chatflow_step(title="IP selection")
    def ip_selection(self):
        self.md_show_update("Deploying Network on Nodes....")
        # deploy network on nodes
        for i in range(len(self.selected_nodes)):
            node = self.selected_nodes[i]
            pool_id = self.selected_pool_ids[i]
            result = deployer.add_network_node(self.network_view.name, node, pool_id, self.network_view)
            if not result:
                continue
            for wid in result["ids"]:
                success = deployer.wait_workload(wid)
                if not success:
                    raise StopChatFlow(f"Failed to add node {node.node_id} to network {wid}")
            self.network_view = self.network_view.copy()

        # get ip addresses
        self.ip_addresses = []
        master_free_ips = self.network_view.get_node_free_ips(self.selected_nodes[0])
        self.ip_addresses.append(
            self.drop_down_choice(
                "Please choose IP Address for Master node", master_free_ips, required=True, default=master_free_ips[0]
            )
        )
        self.network_view.used_ips.append(self.ip_addresses[0])
        for i in range(1, len(self.selected_nodes)):
            free_ips = self.network_view.get_node_free_ips(self.selected_nodes[i])
            self.ip_addresses.append(
                self.drop_down_choice(
                    f"Please choose IP Address for Slave node {i}", free_ips, required=True, default=free_ips[0]
                )
            )
            self.network_view.used_ips.append(self.ip_addresses[i])

    @chatflow_step(title="Confirmation")
    def overview(self):
        self.metadata = {
            "Solution name": self.solution_name,
            "Network": self.network_view.name,
            "Masters count": 1,
            "Slaves count": self.workernodes.value,
            "Cluster size": self.cluster_size,
            "Cluster secret": self.cluster_secret,
            "Nodes IP Addresses": self.ip_addresses,
        }

    @chatflow_step(title="Cluster reservations", disable_previous=True)
    def reservation(self):
        metadata = {
            "name": self.solution_name,
            "form_info": {"chatflow": "kubernetes", "Solution name": self.solution_name},
        }
        metadata["form_info"].update(self.metadata)
        self.reservations = deployer.deploy_kubernetes_cluster(
            pool_id=self.selected_pool_ids[0],
            node_ids=[n.node_id for n in self.selected_nodes],
            network_name=self.network_view.name,
            cluster_secret=self.cluster_secret,
            ssh_keys=self.ssh_keys,
            size=self.cluster_size,
            ip_addresses=self.ip_addresses,
            slave_pool_ids=self.selected_pool_ids[1:],
            solution_uuid=self.solution_id,
            **metadata,
        )

        for resv in self.reservations:
            success = deployer.wait_workload(resv["reservation_id"], self)
            if not success:
                solutions.cancel_solution([resv["reservation_id"]])
                raise StopChatFlow(f"Failed to deploy workload {resv['reservation_id']}")

    @chatflow_step(title="Success", disable_previous=True)
    def success(self):
        res = f"""# Kubernetes cluster has been deployed successfully:
Master reservation id is: {self.reservations[0]["reservation_id"]}
IP: {self.reservations[0]["ip_address"]}
To connect ssh rancher@{self.reservations[0]["ip_address"]}

"""
        for idx, resv in enumerate(self.reservations[1:]):
            res += f"""Worker {idx + 1} reservation id is: {resv["reservation_id"]}
IP: {resv["ip_address"]}
To connect ssh rancher@{resv["ip_address"]}
"""
        self.md_show(res)


chat = kubernetesDeploy
