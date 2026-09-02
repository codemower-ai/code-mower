"""Doctor check registry primitives.

The public CLI remains ``code-mower doctor``. This package keeps the
check/report data model independent from the large command adapter so runtime,
GitHub, provider, cloud, and output checks can move behind registries without
changing the CLI contract.
"""

from .cloud import (
    DEFAULT_CLOUD_TOKEN_DIR,
    DEFAULT_CLOUD_TOKEN_ENV,
    check_cloud_token_surface,
    token_file_mentions_cloud_token,
)
from .adoption import (
    check_adoption_posture_guidance,
    check_adoption_setup,
    config_with_repository_target,
    detect_repo_slug,
    normalize_repo_slug,
    repo_slug_from_remote,
)
from .common import (
    ACTIONS_COST_SAMPLE_DEFAULT,
    ACTIONS_COST_SAMPLE_MAX,
    load_inputs,
)
from .github import check_github_setup
from .github_config import check_repository_posture
from .github_human_token import (
    check_human_automation_token,
    human_automation_token_config,
    human_automation_token_required,
)
from .github_trusted_authors import trusted_author_variable_statuses
from .groups import GROUP_LABELS, doctor_check_group_id
from .models import (
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    DoctorCheck,
    DoctorReport,
)
from .output import doctor_output_group, render_doctor_text
from .presets import (
    apply_first_run_defaults,
    resolve_doctor_config_path,
    resolve_doctor_config_path_for_script,
    resolve_doctor_provider_templates_path,
)
from .providers import (
    check_lane_runtime,
    check_review_hygiene,
    effective_lane,
    provider_template_coverage,
    selected_lanes,
)
from .provider_probe import evaluate_json_probe, local_cli_probe_remediation
from .privacy import auth_probe_output_detail
from .registry import (
    DEFAULT_CHECK_GROUPS,
    BASE_DOCTOR_STAGES,
    OPTIONAL_DOCTOR_STAGES,
    DoctorCheckGroup,
    DoctorCheckStage,
    build_doctor_run_plan,
    default_check_group_ids,
)
from .runtime import (
    check_github_auth_surface,
    check_macos_runner_launchagent,
    check_pytest,
    check_python_runtime,
    check_ripgrep,
)
from .self_hosted_runner import (
    check_runner_actionlint_available,
    check_runner_cli_auth,
    check_runner_generated_workflows_actionlint,
    check_runner_launchagent,
    check_runner_listener_env_freshness,
    check_runner_required_env,
    check_runner_workflow_labels,
    check_self_hosted_runner,
)
from .runner import run_doctor

__all__ = [
    "DEFAULT_CHECK_GROUPS",
    "GROUP_LABELS",
    "BASE_DOCTOR_STAGES",
    "OPTIONAL_DOCTOR_STAGES",
    "ACTIONS_COST_SAMPLE_DEFAULT",
    "ACTIONS_COST_SAMPLE_MAX",
    "DEFAULT_CLOUD_TOKEN_DIR",
    "DEFAULT_CLOUD_TOKEN_ENV",
    "DoctorCheck",
    "DoctorCheckGroup",
    "DoctorCheckStage",
    "DoctorReport",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_SKIP",
    "STATUS_WARN",
    "apply_first_run_defaults",
    "auth_probe_output_detail",
    "build_doctor_run_plan",
    "check_cloud_token_surface",
    "check_adoption_posture_guidance",
    "check_adoption_setup",
    "check_github_auth_surface",
    "check_macos_runner_launchagent",
    "check_github_setup",
    "check_human_automation_token",
    "trusted_author_variable_statuses",
    "check_repository_posture",
    "check_lane_runtime",
    "check_review_hygiene",
    "check_runner_actionlint_available",
    "check_runner_cli_auth",
    "check_runner_generated_workflows_actionlint",
    "check_runner_launchagent",
    "check_runner_listener_env_freshness",
    "check_runner_required_env",
    "check_runner_workflow_labels",
    "check_self_hosted_runner",
    "check_pytest",
    "check_python_runtime",
    "check_ripgrep",
    "config_with_repository_target",
    "default_check_group_ids",
    "detect_repo_slug",
    "doctor_check_group_id",
    "doctor_output_group",
    "effective_lane",
    "evaluate_json_probe",
    "human_automation_token_config",
    "human_automation_token_required",
    "load_inputs",
    "local_cli_probe_remediation",
    "normalize_repo_slug",
    "provider_template_coverage",
    "render_doctor_text",
    "resolve_doctor_config_path",
    "resolve_doctor_config_path_for_script",
    "resolve_doctor_provider_templates_path",
    "repo_slug_from_remote",
    "run_doctor",
    "selected_lanes",
    "token_file_mentions_cloud_token",
]
