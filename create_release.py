
"""Create Jira releases using GitHub repository custom properties."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any
from urllib.parse import quote

import requests
from requests import Response
from requests.auth import HTTPBasicAuth


REQUEST_TIMEOUT = 30

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

JIRA_PROJECTS_PROPERTY = "jira_projects"
DEPLOYMENT_TYPE_PROPERTY = "deployment_type"

DEPLOYMENT_TYPE_DIRECT = "direct"
DEPLOYMENT_TYPE_TEMPLATE = "template"

SUPPORTED_DEPLOYMENT_TYPES = {
    DEPLOYMENT_TYPE_DIRECT,
    DEPLOYMENT_TYPE_TEMPLATE,
}

# Support the current property name and the legacy property name.
WORKFLOW_PROPERTY_NAMES = (
    "workflow",
    "workflows",
)

LOGGER = logging.getLogger(__name__)


# Environment loading and validation.

def required_env(name: str) -> str:
    """Return a required environment variable."""

    value = os.getenv(name, "").strip()

    if not value:
        raise ValueError(
            f"Required environment variable '{name}' is missing "
            "or empty. Configure it in the GitHub Actions workflow "
            "or repository secrets, as appropriate."
        )

    return value


def load_environment() -> dict[str, str]:
    """Load and validate application environment variables."""

    names = (
        "VERSION",
        "TAG",
        "REPOSITORY",
        "REF",
        "JIRA_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "GITHUB_TOKEN",
    )

    # Collect all missing variables to avoid repeated failed runs.
    environment = {
        name: os.getenv(name, "").strip()
        for name in names
    }

    missing = [
        name
        for name, value in environment.items()
        if not value
    ]

    if missing:
        raise ValueError(
            "Environment configuration validation failed. "
            f"Missing or empty variables: {', '.join(missing)}. "
            "Configure these values in GitHub Actions workflow "
            "environment variables or repository secrets."
        )

    environment["JIRA_URL"] = environment["JIRA_URL"].rstrip("/")

    if not environment["JIRA_URL"].startswith(
        ("https://", "http://")
    ):
        raise ValueError(
            "Invalid JIRA_URL. Provide a complete Jira base URL, "
            "for example: https://your-company.atlassian.net."
        )

    if " " in environment["JIRA_URL"]:
        raise ValueError(
            "Invalid JIRA_URL. The Jira base URL must not contain "
            "spaces. Check the configured URL."
        )

    if any(char.isspace() for char in environment["REF"]):
        raise ValueError(
            f"Invalid REF '{environment['REF']}'. "
            "Provide a valid Git branch name or tag name without "
            "whitespace."
        )

    if any(char.isspace() for char in environment["VERSION"]):
        raise ValueError(
            f"Invalid VERSION '{environment['VERSION']}'. "
            "Jira release version names must not contain "
            "leading, trailing, or embedded whitespace."
        )

    if any(char.isspace() for char in environment["TAG"]):
        raise ValueError(
            f"Invalid TAG '{environment['TAG']}'. "
            "Provide a valid Git tag name without whitespace."
        )

    return environment


# GitHub API operations.

def parse_repository(repository: str) -> tuple[str, str]:
    """Validate and split a GitHub owner/repository value."""

    repository = repository.strip()

    if not repository:
        raise ValueError(
            "Repository validation failed: REPOSITORY is empty. "
            "Expected format: owner/repository, for example "
            "Test-Organization7144/TestGithubWorkflow."
        )

    if repository.count("/") != 1:
        raise ValueError(
            f"Invalid REPOSITORY '{repository}'. "
            "Expected exactly one slash in owner/repository format. "
            "Do not include https://github.com/ or a trailing slash."
        )

    owner, repo = (
        part.strip()
        for part in repository.split("/", 1)
    )

    if not owner or not repo:
        raise ValueError(
            f"Invalid REPOSITORY '{repository}'. "
            "Both the GitHub owner and repository name are required."
        )

    if owner in {".", ".."} or repo in {".", ".."}:
        raise ValueError(
            f"Invalid REPOSITORY '{repository}'. "
            "The owner and repository names must be valid identifiers."
        )

    if any(char.isspace() for char in owner + repo):
        raise ValueError(
            f"Invalid REPOSITORY '{repository}'. "
            "The owner and repository names must not contain whitespace."
        )

    return owner, repo


def create_github_session(
    token: str,
) -> requests.Session:
    """Create an authenticated GitHub API session."""

    session = requests.Session()

    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "Content-Type": "application/json",
        }
    )

    return session


def github_request(
    session: requests.Session,
    method: str,
    url: str,
    operation: str,
    **kwargs: Any,
) -> Response:
    """Execute a GitHub API request."""

    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException:
        LOGGER.exception("%s failed.", operation)
        raise

    LOGGER.info(
        "%s status: %s",
        operation,
        response.status_code,
    )

    return response


def log_github_api_error(
    response: Response,
    operation: str,
) -> None:
    """Log details from a failed GitHub API response."""

    LOGGER.error(
        "%s failed with HTTP status %s.",
        operation,
        response.status_code,
    )

    response_text = response.text.strip()

    if response_text:
        # Never log request headers or authentication tokens.
        LOGGER.error(
            "GitHub API response: %s",
            response_text,
        )


def json_value(
    response: Response,
    operation: str,
) -> Any:
    """Return a GitHub response as JSON."""

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"GitHub returned an invalid JSON response during "
            f"{operation}. Check the GitHub API response and retry."
        ) from exc


def get_repository_properties(
    session: requests.Session,
    repository: str,
) -> dict[str, Any]:
    """Retrieve GitHub custom properties for a repository."""

    owner, repo = parse_repository(repository)

    url = (
        f"{GITHUB_API_BASE}/repos/"
        f"{quote(owner, safe='')}/{quote(repo, safe='')}"
        "/properties/values"
    )

    operation = (
        f"GitHub custom property lookup for '{repository}'"
    )

    LOGGER.info("========================================")
    LOGGER.info("Reading GitHub repository custom properties")
    LOGGER.info("Repository: %s", repository)
    LOGGER.info("Endpoint: %s", url)
    LOGGER.info("========================================")

    response = github_request(
        session,
        "GET",
        url,
        operation,
    )

    if not response.ok:
        log_github_api_error(response, operation)

        if response.status_code == 401:
            raise ValueError(
                "GitHub authentication failed (HTTP 401). "
                "Verify that GITHUB_TOKEN is valid and has not expired."
            )

        if response.status_code == 403:
            raise ValueError(
                "GitHub access denied (HTTP 403) while reading "
                f"custom properties for '{repository}'. Check token "
                "permissions, repository access, organization approval, "
                "organization policies, and API rate limits. Review "
                "the logged GitHub API response for further details."
            )

        if response.status_code == 404:
            raise ValueError(
                f"GitHub repository '{repository}' was not found "
                "or is inaccessible to the supplied token. Verify "
                "REPOSITORY, organization membership, and token access."
            )

        if response.status_code == 429:
            raise ValueError(
                "GitHub API rate limit exceeded (HTTP 429). "
                "Wait before retrying and check the token's rate limits."
            )

        response.raise_for_status()

    data = json_value(response, operation)

    if not isinstance(data, list):
        raise ValueError(
            "GitHub repository properties response has an unexpected "
            "format. Expected a list of property_name/value objects. "
            "Verify the GitHub API endpoint and token permissions."
        )

    properties: dict[str, Any] = {}

    for item in data:
        if not isinstance(item, dict):
            LOGGER.warning(
                "Ignoring an invalid custom property entry for "
                "repository '%s'.",
                repository,
            )
            continue

        property_name = item.get("property_name")

        if not isinstance(property_name, str):
            LOGGER.warning(
                "Ignoring a custom property entry without a valid "
                "property_name for repository '%s'.",
                repository,
            )
            continue

        properties[property_name] = item.get("value")

    LOGGER.info(
        "Custom properties found: %s",
        ", ".join(sorted(properties.keys())) or "None",
    )

    return properties


def get_property_value(
    properties: dict[str, Any],
    property_name: str,
    repository: str,
) -> Any:
    """Return a required, non-empty GitHub custom property."""

    if property_name not in properties:
        available = ", ".join(sorted(properties.keys())) or "None"

        raise ValueError(
            f"Configuration validation failed for repository "
            f"'{repository}': required custom property "
            f"'{property_name}' is missing. Available properties: "
            f"{available}. Create the property in GitHub settings "
            "and assign it a valid value."
        )

    value = properties[property_name]

    if value is None:
        raise ValueError(
            f"Configuration validation failed for repository "
            f"'{repository}': custom property '{property_name}' "
            "has a null value. Assign a valid value in GitHub settings."
        )

    if isinstance(value, str) and not value.strip():
        raise ValueError(
            f"Configuration validation failed for repository "
            f"'{repository}': custom property '{property_name}' "
            "is empty. Assign a valid value in GitHub settings."
        )

    if isinstance(value, list) and not value:
        raise ValueError(
            f"Configuration validation failed for repository "
            f"'{repository}': custom property '{property_name}' "
            "contains no values."
        )

    return value


# Deployment type configuration.

def get_deployment_type(
    properties: dict[str, Any],
    repository: str,
) -> str:
    """Resolve the deployment model, defaulting to direct."""

    if DEPLOYMENT_TYPE_PROPERTY not in properties:
        LOGGER.info(
            "Custom property '%s' is not configured for repository "
            "'%s'. Defaulting deployment type to '%s'.",
            DEPLOYMENT_TYPE_PROPERTY,
            repository,
            DEPLOYMENT_TYPE_DIRECT,
        )

        return DEPLOYMENT_TYPE_DIRECT

    value = properties[DEPLOYMENT_TYPE_PROPERTY]

    if value is None:
        LOGGER.info(
            "Custom property '%s' is empty for repository '%s'. "
            "Defaulting deployment type to '%s'.",
            DEPLOYMENT_TYPE_PROPERTY,
            repository,
            DEPLOYMENT_TYPE_DIRECT,
        )

        return DEPLOYMENT_TYPE_DIRECT

    if isinstance(value, list):
        if not value:
            return DEPLOYMENT_TYPE_DIRECT

        if len(value) > 1:
            raise ValueError(
                f"Invalid deployment configuration for repository "
                f"'{repository}': custom property "
                f"'{DEPLOYMENT_TYPE_PROPERTY}' contains multiple values. "
                "Configure exactly one value: 'direct' or 'template'."
            )

        value = value[0]

    deployment_type = str(value).strip().lower()

    if not deployment_type:
        LOGGER.info(
            "Deployment type is empty for repository '%s'. "
            "Defaulting to '%s'.",
            repository,
            DEPLOYMENT_TYPE_DIRECT,
        )

        return DEPLOYMENT_TYPE_DIRECT

    if deployment_type not in SUPPORTED_DEPLOYMENT_TYPES:
        raise ValueError(
            f"Invalid deployment configuration for repository "
            f"'{repository}': custom property "
            f"'{DEPLOYMENT_TYPE_PROPERTY}' has value "
            f"'{deployment_type}'. Supported values are 'direct' "
            "and 'template'. Correct the property in GitHub settings."
        )

    LOGGER.info("Deployment type: %s", deployment_type)

    return deployment_type


# Jira project configuration from repository properties.

def get_jira_projects(
    properties: dict[str, Any],
    repository: str,
) -> list[str]:
    """Read and validate Jira project keys from custom properties."""

    value = get_property_value(
        properties,
        JIRA_PROJECTS_PROPERTY,
        repository,
    )

    if isinstance(value, list):
        projects = [
            str(project).strip()
            for project in value
            if str(project).strip()
        ]
    elif isinstance(value, str):
        projects = [
            project.strip()
            for project in value.split(",")
            if project.strip()
        ]
    else:
        raise ValueError(
            f"Invalid custom property '{JIRA_PROJECTS_PROPERTY}' "
            f"for repository '{repository}'. Expected a comma-separated "
            "string or a list of Jira project keys, for example "
            "'APPDEPLOY,SCRUM'."
        )

    if not projects:
        raise ValueError(
            f"No Jira projects configured in custom property "
            f"'{JIRA_PROJECTS_PROPERTY}' for repository '{repository}'. "
            "Provide at least one valid Jira project key."
        )

    normalized_projects: list[str] = []
    seen_projects: set[str] = set()

    for project in projects:
        if not project:
            continue

        if not project.replace("_", "").isalnum():
            raise ValueError(
                f"Invalid Jira project key '{project}' in custom "
                f"property '{JIRA_PROJECTS_PROPERTY}' for repository "
                f"'{repository}'. Check the configured project keys."
            )

        normalized_key = project.upper()

        if normalized_key in seen_projects:
            LOGGER.warning(
                "Duplicate Jira project key '%s' found in repository "
                "'%s'. Duplicate entry will be ignored.",
                project,
                repository,
            )
            continue

        seen_projects.add(normalized_key)
        normalized_projects.append(normalized_key)

    if not normalized_projects:
        raise ValueError(
            f"No valid Jira project keys remain in custom property "
            f"'{JIRA_PROJECTS_PROPERTY}' for repository '{repository}'."
        )

    LOGGER.info(
        "Jira projects: %s",
        ", ".join(normalized_projects),
    )

    return normalized_projects


# Workflow configuration from repository properties.

def get_workflows(
    properties: dict[str, Any],
    repository: str,
) -> dict[str, str]:
    """Read and validate workflow mappings from custom properties."""

    property_name = next(
        (
            candidate
            for candidate in WORKFLOW_PROPERTY_NAMES
            if candidate in properties
        ),
        None,
    )

    if property_name is None:
        raise ValueError(
            f"Workflow configuration missing for repository "
            f"'{repository}'. Expected GitHub custom property "
            "'workflow' (preferred) or 'workflows' (legacy). "
            "Configure environment mappings or a single workflow filename."
        )

    value = properties[property_name]

    if isinstance(value, list):
        value = "|".join(
            str(item).strip()
            for item in value
            if str(item).strip()
        )

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"Workflow configuration invalid for repository "
            f"'{repository}': custom property '{property_name}' "
            "is empty. Example: "
            "uat=uatdeploy.yml|prod=proddeploy.yml."
        )

    value = value.strip()
    result: dict[str, str] = {}

    # A single workflow filename is supported for direct deployment.
    if "=" not in value:
        workflow = value.strip()

        if not workflow.lower().endswith((".yml", ".yaml")):
            raise ValueError(
                f"Invalid workflow filename '{workflow}' for "
                f"repository '{repository}'. Expected a .yml or "
                ".yaml filename."
            )

        if "/" in workflow or "\\" in workflow:
            raise ValueError(
                f"Invalid workflow filename '{workflow}' for "
                f"repository '{repository}'. Provide a filename from "
                "the .github/workflows directory, not a path."
            )

        return {"default": workflow}

    # Environment-specific mappings support direct and template modes.
    for mapping in value.split("|"):
        mapping = mapping.strip()

        if not mapping:
            raise ValueError(
                f"Invalid workflow configuration for repository "
                f"'{repository}': an empty mapping was found. "
                "Remove extra '|' separators."
            )

        if mapping.count("=") != 1:
            raise ValueError(
                f"Invalid workflow mapping '{mapping}' for repository "
                f"'{repository}'. Expected environment=workflow.yml."
            )

        environment_name, workflow = mapping.split("=", 1)
        environment_name = environment_name.strip().lower()
        workflow = workflow.strip()

        if not environment_name or not workflow:
            raise ValueError(
                f"Invalid workflow mapping '{mapping}' for repository "
                f"'{repository}'. Both environment and workflow "
                "filename are required."
            )

        if not environment_name.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"Invalid environment name '{environment_name}' in "
                f"repository '{repository}'. Use letters, numbers, "
                "hyphens, or underscores."
            )

        if environment_name in result:
            raise ValueError(
                f"Duplicate workflow environment '{environment_name}' "
                f"configured for repository '{repository}'. Each "
                "environment must have exactly one mapping."
            )

        if "/" in workflow or "\\" in workflow:
            raise ValueError(
                f"Invalid workflow filename '{workflow}' for "
                f"environment '{environment_name}' in repository "
                f"'{repository}'. Provide a filename, not a path."
            )

        if not workflow.lower().endswith((".yml", ".yaml")):
            raise ValueError(
                f"Invalid workflow filename '{workflow}' for "
                f"environment '{environment_name}' in repository "
                f"'{repository}'. Expected a .yml or .yaml filename."
            )

        result[environment_name] = workflow

    if not result:
        raise ValueError(
            f"No valid workflow mappings found for repository "
            f"'{repository}'. Configure workflow as "
            "'uat=uatdeploy.yml|prod=proddeploy.yml'."
        )

    LOGGER.info(
        "Deployment workflows: %s",
        ", ".join(
            f"{environment_name}={workflow}"
            for environment_name, workflow in sorted(result.items())
        ),
    )

    return result


# Jira API operations.

def create_jira_session(
    email: str,
    token: str,
) -> requests.Session:
    """Create an authenticated Jira session."""

    session = requests.Session()

    session.auth = HTTPBasicAuth(email, token)

    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    return session


def jira_request(
    session: requests.Session,
    method: str,
    url: str,
    operation: str,
    **kwargs: Any,
) -> Response:
    """Execute a Jira request."""

    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException:
        LOGGER.exception("%s failed.", operation)
        raise

    LOGGER.info(
        "%s status: %s",
        operation,
        response.status_code,
    )

    return response


def log_api_error(
    response: Response,
    operation: str,
) -> None:
    """Log details from a failed Jira API response."""

    LOGGER.error(
        "%s failed with HTTP status %s.",
        operation,
        response.status_code,
    )

    response_text = response.text.strip()

    if response_text:
        LOGGER.error("Jira API response: %s", response_text)


def json_object(
    response: Response,
    operation: str,
) -> dict[str, Any]:
    """Return a Jira response as a JSON object."""

    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(
            f"Jira returned an invalid JSON response during "
            f"{operation}. Check the API response and retry."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Jira returned an unexpected response during {operation}. "
            "Expected a JSON object."
        )

    return data


def get_jira_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
) -> dict[str, Any]:
    """Retrieve a Jira project."""

    operation = f"Jira project lookup for '{project_key}'"

    response = jira_request(
        session,
        "GET",
        f"{jira_url}/rest/api/3/project/{quote(project_key, safe='')}",
        operation,
    )

    if not response.ok:
        log_api_error(response, operation)

        if response.status_code == 401:
            raise ValueError(
                "Jira authentication failed (HTTP 401). "
                "Verify JIRA_EMAIL and JIRA_API_TOKEN."
            )

        if response.status_code == 403:
            raise ValueError(
                f"Jira denied access to project '{project_key}' "
                "(HTTP 403). Check the service account's Jira "
                "project permissions and API access."
            )

        if response.status_code == 404:
            raise ValueError(
                f"Jira project '{project_key}' was not found or is "
                "inaccessible. Verify the project key and the service "
                "account's project access."
            )

        response.raise_for_status()

    return json_object(response, operation)


# Jira release creation and duplicate handling.

def is_duplicate_version(
    response: Response,
) -> bool:
    """Return whether a response indicates an existing Jira version."""

    if response.status_code != 400:
        return False

    response_text = response.text.lower()

    return any(
        indicator in response_text
        for indicator in (
            "already exists",
            "version already exists",
            "a version with this name already exists",
            "name already exists",
        )
    )


def create_jira_version(
    session: requests.Session,
    jira_url: str,
    project_id: int,
    project_key: str,
    version: str,
    description: str,
) -> tuple[str, dict[str, Any] | None]:
    """Create a Jira release/version."""

    operation = f"Jira release creation for '{project_key}'"
    url = f"{jira_url}/rest/api/3/version"

    LOGGER.info("Jira release create endpoint: POST %s", url)

    response = jira_request(
        session,
        "POST",
        url,
        operation,
        json={
            "name": version,
            "description": description,
            "projectId": project_id,
            "released": False,
        },
    )

    if response.ok:
        return (
            "created",
            json_object(response, operation),
        )

    # Preserve duplicate-version handling and continue processing projects.
    if is_duplicate_version(response):
        LOGGER.warning(
            "Release '%s' already exists in Jira project '%s'. "
            "Skipping this project and continuing.",
            version,
            project_key,
        )

        return "exists", None

    log_api_error(response, operation)

    if response.status_code == 400:
        raise ValueError(
            f"Jira rejected release '{version}' for project "
            f"'{project_key}' (HTTP 400). Check the release name, "
            "project ID, description, and Jira API response above."
        )

    if response.status_code == 401:
        raise ValueError(
            "Jira authentication failed while creating release "
            f"'{version}' in project '{project_key}' (HTTP 401). "
            "Verify JIRA_EMAIL and JIRA_API_TOKEN."
        )

    if response.status_code == 403:
        raise ValueError(
            f"Jira denied permission to create release '{version}' "
            f"in project '{project_key}' (HTTP 403). Check the "
            "service account's project and version-management permissions."
        )

    if response.status_code == 404:
        raise ValueError(
            f"Jira could not find a required resource while creating "
            f"release '{version}' in project '{project_key}' (HTTP 404). "
            "Verify the Jira URL and project configuration."
        )

    response.raise_for_status()

    raise RuntimeError(
        f"Unexpected Jira API response while creating release "
        f"'{version}' in project '{project_key}'."
    )


# Jira release metadata serialization.

def build_release_description(
    environment: dict[str, str],
    deployment_type: str,
    workflows: dict[str, str],
) -> str:
    """Build the Jira release description."""

    workflow_text = ",".join(
        f"{environment_name}:{workflow}"
        for environment_name, workflow in sorted(workflows.items())
    )

    return (
        f"version={environment['VERSION']}"
        f"|tag={environment['TAG']}"
        f"|repository={environment['REPOSITORY']}"
        f"|deploymentType={deployment_type}"
        f"|workflows={workflow_text}"
        f"|ref={environment['REF']}"
    )


# Per-project release processing.

def process_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
    version: str,
    description: str,
) -> str:
    """Create a release for one Jira project."""

    LOGGER.info("========================================")
    LOGGER.info("Creating release in Jira project: %s", project_key)

    project = get_jira_project(
        session,
        jira_url,
        project_key,
    )

    try:
        project_id = int(project["id"])
    except KeyError as exc:
        raise ValueError(
            f"Jira project '{project_key}' response does not contain "
            "a project ID. Verify the Jira API response."
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid Jira project ID '{project.get('id')}' "
            f"for project '{project_key}'."
        ) from exc

    LOGGER.info(
        "Jira project: %s | key: %s | ID: %s",
        project.get("name", "Unknown"),
        project.get("key", project_key),
        project_id,
    )

    status, release = create_jira_version(
        session=session,
        jira_url=jira_url,
        project_id=project_id,
        project_key=project_key,
        version=version,
        description=description,
    )

    if status == "exists":
        return status

    if release is None:
        raise ValueError(
            f"Jira release response for project '{project_key}' "
            "did not contain release data."
        )

    LOGGER.info(
        "Release created successfully | project: %s | name: %s | ID: %s",
        project_key,
        release.get("name", version),
        release.get("id", "Unknown"),
    )

    return "created"


# Release configuration and result logging.

def log_configuration(
    environment: dict[str, str],
    deployment_type: str,
    projects: list[str],
    workflows: dict[str, str],
    description: str,
) -> None:
    """Log release configuration."""

    workflow_text = ", ".join(
        f"{environment_name}={workflow}"
        for environment_name, workflow in sorted(workflows.items())
    )

    LOGGER.info("========================================")
    LOGGER.info("Release configuration")
    LOGGER.info("========================================")
    LOGGER.info("Repository       : %s", environment["REPOSITORY"])
    LOGGER.info("Version          : %s", environment["VERSION"])
    LOGGER.info("Tag              : %s", environment["TAG"])
    LOGGER.info("REF              : %s", environment["REF"])
    LOGGER.info("Deployment Type  : %s", deployment_type)
    LOGGER.info("Jira             : %s", environment["JIRA_URL"])
    LOGGER.info("Projects         : %s", ", ".join(projects))
    LOGGER.info("Workflows        : %s", workflow_text)
    LOGGER.info("Description      : %s", description)
    LOGGER.info("========================================")


# Application entry point and orchestration.

def main() -> int:
    """Run the Jira release creation process."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        # Load and validate workflow inputs and credentials.
        environment = load_environment()

        LOGGER.info("Starting Jira release creation.")

        # Initialize authenticated GitHub API access.
        github_session = create_github_session(
            environment["GITHUB_TOKEN"],
        )

        # Load repository-level deployment configuration.
        properties = get_repository_properties(
            github_session,
            environment["REPOSITORY"],
        )

        # Resolve direct or template deployment mode with a direct default.
        deployment_type = get_deployment_type(
            properties,
            environment["REPOSITORY"],
        )

        # Resolve the Jira projects configured for this repository.
        projects = get_jira_projects(
            properties,
            environment["REPOSITORY"],
        )

        # Resolve environment-to-workflow mappings from custom properties.
        workflows = get_workflows(
            properties,
            environment["REPOSITORY"],
        )

        # Serialize release metadata for the Jira version description.
        description = build_release_description(
            environment,
            deployment_type,
            workflows,
        )

        # Log the resolved release configuration for auditability.
        log_configuration(
            environment,
            deployment_type,
            projects,
            workflows,
            description,
        )

        # Initialize authenticated Jira API access.
        jira_session = create_jira_session(
            environment["JIRA_EMAIL"],
            environment["JIRA_API_TOKEN"],
        )

        created: list[str] = []
        existing: list[str] = []

        # Create the release in each configured Jira project.
        for project in projects:
            status = process_project(
                session=jira_session,
                jira_url=environment["JIRA_URL"],
                project_key=project,
                version=environment["VERSION"],
                description=description,
            )

            if status == "created":
                created.append(project)
            else:
                existing.append(project)

        # Report created and already-existing releases.
        LOGGER.info("========================================")
        LOGGER.info("Release processing completed")
        LOGGER.info("Created       : %s", ", ".join(created) or "None")
        LOGGER.info("Already exists: %s", ", ".join(existing) or "None")
        LOGGER.info("Deployment Type: %s", deployment_type)
        LOGGER.info("========================================")

        return 0

    except ValueError as exc:
        LOGGER.error("Validation or configuration error: %s", exc)

    except requests.HTTPError as exc:
        LOGGER.error("API request failed: %s", exc)

    except requests.RequestException as exc:
        LOGGER.error(
            "API connection failed. Check network connectivity, "
            "API availability, and proxy configuration: %s",
            exc,
        )

    except KeyboardInterrupt:
        LOGGER.error("Process interrupted by user.")
        return 130

    except Exception:
        LOGGER.exception(
            "Unexpected error while creating Jira releases."
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
