"""CDK construct for the junior growth-tracking tables and Lambdas.

Drop this into the existing stack that owns the shared trainee table, so the
new tables sit beside it and the Lambdas can read it:

    from juniors.juniors_construct import JuniorsConstruct

    juniors = JuniorsConstruct(self, "Juniors", trainees_table=trainees_table)
    # then wire juniors.measurement_fn, juniors.consent_submit_fn and
    # juniors.consent_withdraw_fn into the existing HTTP API routes.

The measurement and withdraw routes sit behind the existing Cognito
authoriser. The consent submit route is public: the single-use token in the
emailed link is the authorisation.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

# The Lambda asset is the folder containing the `juniors` package.
_ASSET_ROOT = str(Path(__file__).resolve().parent.parent)


def _table(scope: Construct, cid: str, name: str, pk: str, sk: str | None = None,
           ttl: str | None = None) -> ddb.Table:
    return ddb.Table(
        scope, cid,
        table_name=name,
        partition_key=ddb.Attribute(name=pk, type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name=sk, type=ddb.AttributeType.STRING) if sk else None,
        billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
        encryption=ddb.TableEncryption.AWS_MANAGED,
        point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
            point_in_time_recovery_enabled=True),
        deletion_protection=True,
        removal_policy=RemovalPolicy.RETAIN,
        time_to_live_attribute=ttl,
    )


class JuniorsConstruct(Construct):
    def __init__(self, scope: Construct, cid: str, *, trainees_table: ddb.ITable) -> None:
        super().__init__(scope, cid)

        # Append-only consent log. SK = "<consent_type>#<granted_at>".
        self.consents = _table(self, "Consents", "JuniorConsents", "trainee_id", "sk")

        # Single-use emailed links. PK = sha256 of the token; rows expire via TTL.
        self.consent_requests = _table(
            self, "ConsentRequests", "JuniorConsentRequests", "token_hash",
            ttl="expires_at")

        # SK = "<measured_on>#<measurement_id>" so a query returns visits in date order.
        self.measurements = _table(
            self, "Measurements", "GrowthMeasurements", "trainee_id", "sk")

        env = {
            "TRAINEES_TABLE": trainees_table.table_name,
            "CONSENTS_TABLE": self.consents.table_name,
            "CONSENT_REQUESTS_TABLE": self.consent_requests.table_name,
            "MEASUREMENTS_TABLE": self.measurements.table_name,
        }
        code = lambda_.Code.from_asset(
            _ASSET_ROOT,
            exclude=["**/tests/**", "**/__pycache__/**", "**/*_construct.py", "cdk.out"],
        )

        def fn(fid: str, handler: str) -> lambda_.Function:
            return lambda_.Function(
                self, fid,
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler=f"juniors.handler.{handler}",
                code=code,
                environment=env,
                timeout=Duration.seconds(10),
                memory_size=256,
            )

        self.measurement_fn = fn("MeasurementFn", "measurement_handler")
        self.consent_submit_fn = fn("ConsentSubmitFn", "consent_submit_handler")
        self.consent_withdraw_fn = fn("ConsentWithdrawFn", "consent_withdraw_handler")

        # Least privilege per function.
        trainees_table.grant_read_data(self.measurement_fn)
        self.consents.grant_read_data(self.measurement_fn)
        self.measurements.grant_read_write_data(self.measurement_fn)

        trainees_table.grant_write_data(self.consent_submit_fn)  # mid-parent height only
        self.consent_requests.grant_read_write_data(self.consent_submit_fn)
        self.consents.grant_write_data(self.consent_submit_fn)

        self.consents.grant_read_write_data(self.consent_withdraw_fn)
