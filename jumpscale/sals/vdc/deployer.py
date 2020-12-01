import hashlib
from jumpscale.sals.vdc.models import KubernetesRole
from jumpscale.clients.explorer.models import ZdbNamespace, K8s, Volume, Container, DiskType
import math

import gevent
from jumpscale.loader import j
from jumpscale.sals.chatflows.chatflows import GedisChatBot
from jumpscale.sals.kubernetes import Manager
from jumpscale.sals.reservation_chatflow import deployer, solutions
from jumpscale.sals.zos import get as get_zos

from .kubernetes import VDCKubernetesDeployer
from .proxy import VDCProxy
from .s3 import VDCS3Deployer
from .monitoring import VDCMonitoring
from .threebot import VDCThreebotDeployer
from .public_ip import VDCPublicIP
from .scheduler import GlobalScheduler, Scheduler
from .size import *

VDC_IDENTITY_FORMAT = "vdc_{}_{}"  # tname, vdc_name
IP_VERSION = "IPv4"
IP_RANGE = "10.200.0.0/16"
MARKETPLACE_HELM_REPO_URL = "https://threefoldtech.github.io/marketplace-charts/"
NO_DEPLOYMENT_BACKUP_NODES = 0


class VDCDeployer:
    def __init__(
        self, password: str, vdc_instance, bot: GedisChatBot = None, proxy_farm_name: str = None,
    ):
        self.vdc_instance = vdc_instance
        self.vdc_name = self.vdc_instance.vdc_name
        self.flavor = self.vdc_instance.flavor
        self.tname = j.data.text.removesuffix(self.vdc_instance.owner_tname, ".3bot")
        self.bot = bot
        self.identity = None
        self.password = password
        self.password_hash = hashlib.md5(self.password.encode()).hexdigest()
        self.email = f"vdc_{self.vdc_instance.solution_uuid}"
        self.wallet_name = self.vdc_instance.wallet.instance_name
        self.proxy_farm_name = proxy_farm_name
        self.vdc_uuid = self.vdc_instance.solution_uuid
        self.description = j.data.serializers.json.dumps({"vdc_uuid": self.vdc_uuid})
        self._log_format = f"VDC: {self.vdc_uuid} NAME: {self.vdc_name}: OWNER: {self.tname} {{}}"
        self._generate_identity()
        if not self.vdc_instance.identity_tid:
            self.vdc_instance.identity_tid = self.identity.tid
            self.vdc_instance.save()
        self._zos = None
        self._wallet = None
        self._kubernetes = None
        self._s3 = None
        self._proxy = None
        self._ssh_key = None
        self._vdc_k8s_manager = None
        self._threebot = None
        self._monitoring = None
        self._public_ip = None

    @property
    def public_ip(self):
        if not self._public_ip:
            self._public_ip = VDCPublicIP(self)
        return self._public_ip

    @property
    def monitoring(self):
        if not self._monitoring:
            self._monitoring = VDCMonitoring(self)
        return self._monitoring

    @property
    def threebot(self):
        if not self._threebot:
            self._threebot = VDCThreebotDeployer(self)
        return self._threebot

    @property
    def vdc_k8s_manager(self):
        if not self._vdc_k8s_manager:
            config_path = f"{j.core.dirs.CFGDIR}/vdc/kube/{self.tname}/{self.vdc_name}.yaml"
            self._vdc_k8s_manager = Manager(config_path)
        return self._vdc_k8s_manager

    @property
    def kubernetes(self):
        if not self._kubernetes:
            self._kubernetes = VDCKubernetesDeployer(self)
        return self._kubernetes

    @property
    def s3(self):
        if not self._s3:
            self._s3 = VDCS3Deployer(self)
        return self._s3

    @property
    def proxy(self):
        if not self._proxy:
            self._proxy = VDCProxy(self, self.proxy_farm_name)
        return self._proxy

    @property
    def wallet(self):
        if not self._wallet:
            wallet_name = self.wallet_name or j.core.config.get("VDC_WALLET")
            self._wallet = j.clients.stellar.get(wallet_name)
        return self._wallet

    @property
    def explorer(self):
        if self.identity:
            return self.identity.explorer
        return j.core.identity.me.explorer

    @property
    def zos(self):
        if not self._zos:
            self._zos = get_zos(self.identity.instance_name)
        return self._zos

    @property
    def ssh_key(self):
        if not self._ssh_key:
            self._ssh_key = j.clients.sshkey.get(self.vdc_name)
            vdc_key_path = j.core.config.get("VDC_KEY_PATH", "~/.ssh/id_rsa")
            self._ssh_key.private_key_path = j.sals.fs.expanduser(vdc_key_path)
            self._ssh_key.load_from_file_system()
        return self._ssh_key

    def _generate_identity(self):
        # create a user identity from an old one or create a new one
        username = VDC_IDENTITY_FORMAT.format(self.tname, self.vdc_name)
        words = j.data.encryption.key_to_mnemonic(self.password_hash.encode())
        identity_name = f"vdc_ident_{self.vdc_uuid}"
        self.identity = j.core.identity.get(
            identity_name, tname=username, email=self.email, words=words, explorer_url=j.core.identity.me.explorer_url
        )
        try:
            self.identity.register()
            self.identity.save()
        except Exception as e:
            j.logger.error(f"failed to generate identity for user {identity_name} due to error {str(e)}")
            raise j.exceptions.Runtime(f"failed to generate identity for user {identity_name} due to error {str(e)}")

    def get_pool_id(self, farm_name, cus=0, sus=0, ipv4us=0):
        farm = self.explorer.farms.get(farm_name=farm_name)
        for pool in self.zos.pools.list():
            farm_id = deployer.get_pool_farm_id(pool.pool_id, pool, self.identity.instance_name)
            if farm_id == farm.id:
                # extend
                if not any([cus, sus, ipv4us]):
                    return pool.pool_id
                node_ids = [node.node_id for node in self.zos.nodes_finder.nodes_search(farm.id)]
                pool_info = self.zos.pools.extend(pool.pool_id, cus, sus, ipv4us, node_ids=node_ids)
                self.zos.billing.payout_farmers(self.wallet, pool_info)
                return pool.pool_id
        pool_info = self.zos.pools.create(cus, sus, ipv4us, farm_name)
        self.zos.billing.payout_farmers(self.wallet, pool_info)
        return pool_info.reservation_id

    def init_vdc(self, selected_farm=PREFERED_FARM):
        """
        1- create 2 pool on storage farms (with the required capacity to have 50-50) as speced
        2- get (and extend) or create a pool for kubernetes controller on the network farm with small flavor
        3- get (and extend) or create a pool for kubernetes workers
        """
        duration = VDC_FLAFORS[self.flavor]["duration"]

        def get_cloud_units(workload):
            ru = workload.resource_units()
            cloud_units = ru.cloud_units()
            return cloud_units.cu * 60 * 60 * 24 * duration, cloud_units.su * 60 * 60 * 24 * duration

        # create zdb pools
        if len(ZDB_FARMS) != 2:
            raise j.exceptions.Validation("incorrect config for ZDB_FARMS in size")
        for farm_name in ZDB_FARMS:
            zdb = ZdbNamespace()
            zdb.size = ZDB_STARTING_SIZE
            zdb.disk_type = DiskType.SSD
            _, sus = get_cloud_units(zdb)
            sus = sus * (S3_NO_DATA_NODES + S3_NO_PARITY_NODES) / 2
            pool_id = self.get_pool_id(farm_name, 0, sus)
            self.wait_pool_payment(pool_id)

        # create kubernetes controller with small flavor on network farm
        k8s = K8s()
        k8s.size = K8SNodeFlavor.SMALL.value
        cus, sus = get_cloud_units(k8s)
        ipv4us = duration * 60 * 60 * 24
        pool_id = self.get_pool_id(NETWORK_FARM, cus, sus, ipv4us)
        self.wait_pool_payment(pool_id, trigger_cus=1)

        # create minio and threebot pool
        cont1 = Container()
        cont1.capacity.cpu = MINIO_CPU
        cont1.capacity.memory = MINIO_MEMORY
        cont1.capacity.disk_size = 256
        cont1.capacity.disk_type = DiskType.SSD
        cont2 = Container()
        cont2.capacity.cpu = THREEBOT_CPU
        cont2.capacity.memory = THREEBOT_MEMORY
        cont2.capacity.disk_size = THREEBOT_DISK
        cont2.capacity.disk_type = DiskType.SSD
        vol = Volume()
        vol.size = int(MINIO_DISK / 1024)
        vol.type = DiskType.SSD
        cus = sus = 0
        for workload in [cont1, cont2, vol]:
            n_cus, n_sus = get_cloud_units(workload)
            cus += n_cus
            sus += n_sus
        pool_id = self.get_pool_id(selected_farm, cus, sus)
        self.wait_pool_payment(pool_id, trigger_cus=1)

    def deploy_vdc_network(self):
        """
        create a network for the vdc on any pool withing the ones created during initialization
        """
        for pool in self.zos.pools.list():
            scheduler = Scheduler(pool_id=pool.pool_id)
            network_success = False
            for access_node in scheduler.nodes_by_capacity(ip_version=IP_VERSION):
                self.info(f"deploying network on node {access_node.node_id}")
                network_success = True
                result = deployer.deploy_network(
                    self.vdc_name, access_node, IP_RANGE, IP_VERSION, pool.pool_id, self.identity.instance_name,
                )
                for wid in result["ids"]:
                    try:
                        success = deployer.wait_workload(
                            wid,
                            breaking_node_id=access_node.node_id,
                            identity_name=self.identity.instance_name,
                            bot=self.bot,
                            cancel_by_uuid=False,
                            expiry=5,
                        )
                        network_success = network_success and success
                    except Exception as e:
                        network_success = False
                        self.error(f"network workload {wid} failed on node {access_node.node_id} due to error {str(e)}")
                        break
                if network_success:
                    self.info(
                        f"saving wireguard config to {j.core.dirs.CFGDIR}/vdc/wireguard/{self.tname}/{self.vdc_name}.conf"
                    )
                    # store wireguard config
                    wg_quick = result["wg"]
                    j.sals.fs.mkdirs(f"{j.core.dirs.CFGDIR}/vdc/wireguard/{self.tname}")
                    j.sals.fs.write_file(
                        f"{j.core.dirs.CFGDIR}/vdc/wireguard/{self.tname}/{self.vdc_name}.conf", wg_quick
                    )
                    return True

    def deploy_vdc_zdb(self, scheduler=None):
        """
        1- get pool_id of each farm from ZDB_FARMS
        2- deploy zdbs on it with half of the capacity
        """
        gs = scheduler or GlobalScheduler()
        zdb_threads = []
        for farm in ZDB_FARMS:
            pool_id = self.get_pool_id(farm)
            zdb_threads.append(
                gevent.spawn(
                    self.s3.deploy_s3_zdb,
                    pool_id=pool_id,
                    scheduler=gs,
                    storage_per_zdb=ZDB_STARTING_SIZE,
                    password=self.password,
                    solution_uuid=self.vdc_uuid,
                    no_nodes=(S3_NO_DATA_NODES + S3_NO_PARITY_NODES) / 2,
                )
            )
        return zdb_threads

    def deploy_vdc_kubernetes(self, farm_name, scheduler, cluster_secret):
        """
        1- deploy master
        2- extend cluster with the flavor no_nodes
        """
        gs = scheduler or GlobalScheduler()
        master_pool_id = self.get_pool_id(NETWORK_FARM)
        nv = deployer.get_network_view(self.vdc_name, identity_name=self.identity.instance_name)
        master_ip = self.kubernetes.deploy_master(
            master_pool_id,
            gs,
            K8SNodeFlavor.SMALL,
            cluster_secret,
            [self.ssh_key.public_key.strip()],
            self.vdc_uuid,
            nv,
        )
        if not master_ip:
            return
        no_nodes = VDC_FLAFORS[self.flavor]["k8s"]["no_nodes"]
        wids = self.kubernetes.extend_cluster(
            farm_name,
            master_ip,
            VDC_FLAFORS[self.flavor]["k8s"]["size"],
            cluster_secret,
            [self.ssh_key.public_key.strip()],
            no_nodes,
            VDC_FLAFORS[self.flavor]["duration"],
            solution_uuid=self.vdc_uuid,
        )
        return wids

    def deploy_vdc(self, cluster_secret, minio_ak, minio_sk, farm_name=PREFERED_FARM):
        """deploys a new vdc
        Args:
            cluster_secret: secretr for k8s cluster. used to join nodes in the cluster (will be stored in woprkload metadata)
            minio_ak: access key for minio
            minio_sk: secret key for minio
            farm_name: where to initialize the vdc
        """
        self.info(f"deploying vdc flavor: {self.flavor} farm: {farm_name}")
        if len(minio_ak) < 3 or len(minio_sk) < 8:
            raise j.exceptions.Validation(
                "Access key length should be at least 3, and secret key length at least 8 characters"
            )

        # initialize vdc pools
        self.init_vdc(farm_name)
        if not self.deploy_vdc_network():
            self.error("failed to deploy network")
            return False
        gs = GlobalScheduler()

        # deploy zdbs for s3
        deployment_threads = self.deploy_vdc_zdb(gs)

        # deploy k8s cluster
        k8s_thread = gevent.spawn(self.deploy_vdc_kubernetes, farm_name, gs, cluster_secret)
        deployment_threads.append(k8s_thread)
        gevent.joinall(deployment_threads)
        for thread in deployment_threads:
            if thread.value:
                continue
            self.error(f"failed to deploy vdc. cancelling workloads with uuid {self.vdc_uuid}")
            self.rollback_vdc_deployment()
            return False

        zdb_wids = deployment_threads[0].value + deployment_threads[1].value
        scheduler = Scheduler(farm_name)
        pool_id = self.get_pool_id(farm_name)

        # deploy minio container
        minio_wid = self.s3.deploy_s3_minio_container(
            pool_id,
            minio_ak,
            minio_sk,
            self.ssh_key.public_key.strip(),
            scheduler,
            zdb_wids,
            self.vdc_uuid,
            self.password,
        )
        self.info(f"minio_wid: {minio_wid}")
        if not minio_wid:
            self.error(f"failed to deploy vdc. cancelling workloads with uuid {self.vdc_uuid}")
            self.rollback_vdc_deployment()
            return False

        # deploy threebot container
        threebot_wid = self.threebot.deploy_threebot(minio_wid, pool_id)
        self.info(f"threebot_wid: {threebot_wid}")
        if not threebot_wid:
            self.error(f"failed to deploy vdc. cancelling workloads with uuid {self.vdc_uuid}")
            self.rollback_vdc_deployment()
            return False

        # get kubernetes info
        self.vdc_instance.load_info()
        master_ip = None
        for node in self.vdc_instance.kubernetes:
            if node.role != KubernetesRole.MASTER:
                continue
            master_ip = node.public_ip

        if not master_ip:
            self.error("couldn't get kubernetes master public ip")
            self.rollback_vdc_deployment()

        # download kube config from master
        kube_config = self.kubernetes.download_kube_config(master_ip)

        # deploy monitoring stack on kubernetes
        try:
            self.monitoring.deploy_stack()
        except j.exceptions.Runtime as e:
            # TODO: rollback
            self.error(f"failed to deploy monitoring stack on vdc cluster due to error {str(e)}")

        return kube_config

    def rollback_vdc_deployment(self):
        solutions.cancel_solution_by_uuid(self.vdc_uuid, self.identity.instance_name)
        nv = deployer.get_network_view(self.vdc_name, identity_name=self.identity.instance_name)
        if nv:
            solutions.cancel_solution(
                [workload.id for workload in nv.network_workloads], identity_name=self.identity.instance_name
            )

    def wait_pool_payment(self, pool_id, exp=5, trigger_cus=0, trigger_sus=1):
        expiration = j.data.time.now().timestamp + exp * 60
        while j.data.time.get().timestamp < expiration:
            pool = self.zos.pools.get(pool_id)
            if pool.cus >= trigger_cus and pool.sus >= trigger_sus:
                return True
            gevent.sleep(2)
        return False

    def _log(self, msg, loglevel="info"):
        getattr(j.logger, loglevel)(self._log_format.format(msg))

    def info(self, msg):
        self._log(msg, "info")

    def error(self, msg):
        self._log(msg, "error")

    def renew_plan(self, duration):
        """before calling
        transfer current balance in vdc wallet to deployer wallet
        transfer all amount of new payment to the vdc wallet (amount of package + amount of external nodes)
        """
        self.vdc_instance.load_info()
        pool_ids = set()
        for zdb in self.vdc_instance.s3.zdbs:
            pool_ids.add(zdb.pool_id)
        for k8s in self.vdc_instance.kubernetes:
            pool_ids.add(k8s.pool_id)
        if self.vdc_instance.s3.minio.pool_id:
            pool_ids.add(self.vdc_instance.s3.minio.pool_id)
        if self.vdc_instance.threebot.pool_id:
            pool_ids.add(self.vdc_instance.threebot.pool_id)
        self.info(f"renew plan with pools: {pool_ids}")
        for pool_id in pool_ids:
            pool = self.zos.pools.get(pool_id)
            sus = pool.active_su * duration * 60 * 60 * 24
            cus = pool.active_cu * duration * 60 * 60 * 24
            ipv4us = pool.ipv4us * duration * 60 * 60 * 24
            pool_info = self.zos.pools.extend(pool_id, cus, sus, ipv4us)
            self.info(
                f"renew plan: extending pool {pool_id}, sus: {sus}, cus: {cus}, reservation_id: {pool_info.reservation_id}"
            )
            self.zos.billing.payout_farmers(self.wallet, pool_info)
        self.vdc_instance.expiration = j.data.time.utcnow().timestamp + duration * 60 * 60 * 24
        self.vdc_instance.updated = j.data.time.utcnow().timestamp
        if self.vdc_instance.is_blocked:
            self.vdc_instance.undo_grace_period_action()