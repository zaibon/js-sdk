import pytest
from random import randint
from tests.frontend.pages.solutions.network import Network
from tests.frontend.tests.base_tests import BaseTest


@pytest.mark.integration
class SolutionsTests(BaseTest):
    def test01_deploy_and_delete_network_solution(self):
        """
        Test case for deploying network solution.
        **Test Scenario**

        #. Deploy network solution instance.
        #. Check that network workload has been deployed successfully.
        #. Delete the network solution instance.
        #. Check that network workload has been deleted successfully.
        """

        self.network = Network(self.driver)
        self.network.load()

        self.info("Deploy network solution instance")

        network_name = "network{}".format(randint(1, 500))
        self.network.deploy_network_solution(network_name)

        self.info("Check that network workload has been deployed successfully")
        my_network_instances = self.network.view_my_workloads()

        self.assertIn(network_name, my_network_instances)

        self.info("Delete the network solution instance")
        self.network.delete_network_workload(network_name)

        self.info("Check that network workload has been deleted successfully")
        my_network_instances = self.network.view_my_workloads()
        self.assertNotIn(network_name, my_network_instances)
