import pytest

from cumulusci.tasks.bulkdata import LoadData
from cumulusci.tasks.bulkdata.step import DataOperationStatus


class TestUpsert:
    # bulk API not supported by VCR yet
    @pytest.mark.needs_org()
    def test_upsert_external_id_field(
        self, create_task, cumulusci_test_repo_root, sf, delete_data_from_org
    ):
        delete_data_from_org(["Entitlement", "Opportunity", "Contact", "Account"])

        task = create_task(
            LoadData,
            {
                "sql_path": cumulusci_test_repo_root / "datasets/upsert_example.sql",
                "mapping": cumulusci_test_repo_root / "datasets/upsert_mapping.yml",
            },
        )
        task()
        result = task.return_values
        assert all(
            val["status"] == DataOperationStatus.SUCCESS
            for val in result["step_results"].values()
        ), result.values()
        accounts = sf.query("select Name from Account")
        accounts = {account["Name"] for account in accounts["records"]}
        assert "Sitwell-Bluth" in accounts

        task = create_task(
            LoadData,
            {
                "sql_path": cumulusci_test_repo_root / "datasets/upsert_example_2.sql",
                "mapping": cumulusci_test_repo_root / "datasets/upsert_mapping.yml",
            },
        )
        task()
        accounts = sf.query("select Name from Account")
        accounts = {account["Name"] for account in accounts["records"]}

        assert "Sitwell-Bluth" not in accounts
        assert "Bluth-Sitwell" in accounts

        task = create_task(
            LoadData,
            {
                "sql_path": cumulusci_test_repo_root
                / "datasets/upsert_example_3__opportunities_on_name.sql",
                "mapping": cumulusci_test_repo_root / "datasets/upsert_mapping.yml",
            },
        )
        task()
        result = task.return_values
        opportunities = sf.query("select Name, CloseDate from Opportunity")
        close_dates = {
            opp["Name"]: opp["CloseDate"] for opp in opportunities["records"]
        }
        assert close_dates["represent Opportunity"] == "2022-01-01"
        assert close_dates["another Opportunity"] == "2022-01-01"
