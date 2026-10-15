"""Create Jira releases using GitHub repository custom properties."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from requests import Response
from requests.auth import HTTPBasicAuth


REQUEST_TIMEOUT = 30

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

JIRA_PROJECTS_PROPERTY = "jira_projects"

# Deployment model and supported repository types.
#
# Supported values:
#
#     direct
#     template
#
# The property is optional for backward compatibility.
#
# If the property does not exist, the repository is treated as:
#
#     direct
#
DEPLOYMENT_TYPE_PROPERTY = "deployment_type"

DEPLOYMENT_TYPE_DIRECT = "direct"
DEPLOYMENT_TYPE_TEMPLATE = "template"

SUPPORTED_DEPLOYMENT_TYPES = {
    DEPLOYMENT_TYPE_DIRECT,
    DEPLOYMENT_TYPE_TEMPLATE,
}


# GitHub workflow custom-property names.
#
# Current GitHub Custom Property:
#
#     workflow
#
# "workflows" is also supported for backward compatibility.
#
WORKFLOW_PROPERTY_NAMES = (
    "workflow",
    "workflows",
)

LOGGER = logging.getLogger(__name__)


# Environment loading and validation.

def required_env(name: str) -> str:
    """Return a required environment variable without exposing its value."""

    value = os.getenv(name, "").strip()

    if not value:
        raise ValueError(
            f"Required environment variable '{name}' is missing or empty."
        )

    if any(ord(character) < 32 for character in value):
        raise ValueError(
            f"Environment variable '{name}' must not contain control characters."
        )

    return value


def validate_metadata_value(name: str, value: str) -> None:
    """Reject values that would corrupt the pipe-delimited Jira description."""

    if not value.strip():
        raise ValueError(f"{name} cannot be empty.")

    if "|" in value:
        raise ValueError(
            f"{name} must not contain '|', because it is the Jira metadata delimiter."
        )

    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters.")


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

    environment = {
        name: required_env(name)
        for name in names
    }

    # Validate metadata before it is serialized into the Jira version description.
    for name in ("VERSION", "TAG", "REPOSITORY", "REF"):
        validate_metadata_value(name, environment[name])

    # Accept fully qualified Git refs but store the branch or tag name consistently.
    if environment["REF"].startswith("refs/heads/"):
        environment["REF"] = environment["REF"][len("refs/heads/"):]
    elif environment["REF"].startswith("refs/tags/"):
        environment["REF"] = environment["REF"][len("refs/tags/"):]

    if not environment["REF"] or environment["REF"].startswith("-"):
        raise ValueError(
            "REF must be a valid branch or tag name and must not start with '-'."
        )

    if any(character.isspace() for character in environment["REF"]):
        raise ValueError(
            "REF must not contain whitespace. Use a valid branch or tag name."
        )

    if any(character.isspace() for character in environment["TAG"]):
        raise ValueError(
            "TAG must not contain whitespace. Use a valid Git tag name."
        )

    environment["JIRA_URL"] = environment["JIRA_URL"].rstrip("/")
    jira_url = urlsplit(environment["JIRA_URL"])

    if (
        jira_url.scheme != "https"
        or not jira_url.netloc
        or jira_url.query
        or jira_url.fragment
    ):
        raise ValueError(
            "JIRA_URL must be an HTTPS base URL without a query string or fragment."
        )

    return environment


# GitHub API operations.

def parse_repository(repository: str) -> tuple[str, str]:
    """
    Split GitHub repository into owner and repository name.

    Example:

        Test-Organization7144/TestGithubWorkflow

    Returns:

        ("Test-Organization7144", "TestGithubWorkflow")
    """

    repository = repository.strip()

    if not repository:
        raise ValueError(
            "GitHub repository cannot be empty."
        )

    parts = repository.split("/")

    if len(parts) != 2:
        raise ValueError(
            f"Invalid GitHub repository '{repository}'. "
            "Expected format: owner/repository."
        )

    owner = parts[0].strip()
    repo = parts[1].strip()

    if not owner or not repo:
        raise ValueError(
            f"Invalid GitHub repository '{repository}'. "
            "Expected format: owner/repository."
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
        LOGGER.exception(
            "%s failed.",
            operation,
        )
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
            f"Invalid JSON returned by {operation}."
        ) from exc


def get_repository_properties(
    session: requests.Session,
    repository: str,
) -> dict[str, Any]:
    """
    Retrieve GitHub custom properties for a repository.

    Endpoint:

        GET /repos/{owner}/{repo}/properties/values
    """

    owner, repo = parse_repository(repository)

    url = (
        f"{GITHUB_API_BASE}"
        f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/properties/values"
    )

    operation = (
        f"GitHub custom property lookup for '{repository}'"
    )

    LOGGER.info(
        "========================================"
    )

    LOGGER.info(
        "Reading GitHub repository custom properties"
    )

    LOGGER.info(
        "Repository: %s",
        repository,
    )

    LOGGER.info(
        "Endpoint: %s",
        url,
    )

    LOGGER.info(
        "========================================"
    )

    response = github_request(
        session,
        "GET",
        url,
        operation,
    )

    if not response.ok:
        log_github_api_error(
            response,
            operation,
        )

        if response.status_code == 401:
            raise ValueError(
                "GitHub App installation token authentication failed while "
                "reading repository custom properties. Check that the GitHub "
                "App ID and private key are correct, that the App is installed "
                "on the organization owning the repository, and that the "
                "workflow passes the generated installation token as GITHUB_TOKEN."
            )

        if response.status_code == 403:
            # GitHub may return 403 when the API rate limit has been exhausted.
            if response.headers.get("X-RateLimit-Remaining") == "0":
                raise ValueError(
                    "GitHub API rate limit exceeded while reading repository "
                    "properties. Wait for the rate limit to reset and rerun "
                    "the workflow."
                )

            raise ValueError(
                "GitHub denied access to repository custom properties. Verify "
                "that the GitHub App is installed on the organization owning "
                "the repository, that the repository is included in the App "
                "installation, and that the App has the required repository "
                "metadata/custom-property access. Also check organization "
                "policies and the installation token's repository scope."
            )

        if response.status_code == 404:
            raise ValueError(
                f"GitHub repository '{repository}' was not found or the "
                "GitHub App installation token cannot access it. Verify the "
                "repository owner/name, App installation, and repository access."
            )

        if response.status_code == 429:
            raise ValueError(
                "GitHub API rate limit exceeded while reading repository properties. "
                "Wait and rerun the workflow."
            )

        if response.status_code >= 500:
            raise ValueError(
                f"GitHub API is temporarily unavailable (HTTP {response.status_code}). "
                "Retry the workflow later."
            )

        response.raise_for_status()

    data = json_value(
        response,
        operation,
    )

    if not isinstance(data, list):
        raise ValueError(
            "GitHub repository properties response must be a list."
        )

    properties: dict[str, Any] = {}

    for item in data:
        if not isinstance(item, dict):
            continue

        property_name = item.get("property_name")

        if not isinstance(property_name, str):
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
    """Return a required GitHub custom property."""

    if property_name not in properties:
        raise ValueError(
            f"GitHub custom property '{property_name}' "
            f"is not configured for repository '{repository}'."
        )

    value = properties[property_name]

    if value is None:
        raise ValueError(
            f"GitHub custom property '{property_name}' "
            f"for repository '{repository}' is empty."
        )

    return value


# Deployment type configuration.

def get_deployment_type(
    properties: dict[str, Any],
    repository: str,
) -> str:
    """
    Read the repository deployment model.

    Supported values:

        direct
        template

    The property is optional.

    If it is not configured, the repository is treated as
    a direct workflow repository.

    Examples:

        deployment_type = direct

        deployment_type = template
    """

    # Backward compatibility: existing repositories default to direct.

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

    # GitHub custom properties may be returned as strings.
    # Handle a list defensively as well.
    if isinstance(value, list):
        if not value:
            return DEPLOYMENT_TYPE_DIRECT

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
            f"Invalid GitHub custom property "
            f"'{DEPLOYMENT_TYPE_PROPERTY}'='{deployment_type}' "
            f"for repository '{repository}'. "
            f"Supported values are: "
            f"{', '.join(sorted(SUPPORTED_DEPLOYMENT_TYPES))}."
        )

    LOGGER.info(
        "Deployment type: %s",
        deployment_type,
    )

    return deployment_type


# Jira project configuration from repository properties.

def get_jira_projects(
    properties: dict[str, Any],
    repository: str,
) -> list[str]:
    """
    Read Jira project keys from the GitHub custom property.

    Expected:

        jira_projects = APPDEPLOY,SCRUM
    """

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
    else:
        projects = [
            project.strip()
            for project in str(value).split(",")
            if project.strip()
        ]

    if not projects:
        raise ValueError(
            f"No Jira projects configured in custom property "
            f"'{JIRA_PROJECTS_PROPERTY}' for repository "
            f"'{repository}'."
        )

    invalid_projects = [
        project for project in projects
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project)
    ]

    if invalid_projects:
        raise ValueError(
            "Invalid Jira project key(s): " + ", ".join(invalid_projects) + ". "
            "Use uppercase letters, numbers, and underscores, starting with a letter."
        )

    if len(set(projects)) != len(projects):
        raise ValueError(
            f"Duplicate Jira project keys are configured for repository '{repository}'."
        )

    LOGGER.info(
        "Jira projects: %s",
        ", ".join(projects),
    )

    return projects


# Workflow configuration from repository properties.

def get_workflows(
    properties: dict[str, Any],
    repository: str,
) -> dict[str, str]:
    """
    Read deployment workflows from the GitHub custom property.

    Current property name:

        workflow

    Example:

        workflow = uat=uatdeploy.yml|prod=proddeploy.yml

    Result:

        {
            "uat": "uatdeploy.yml",
            "prod": "proddeploy.yml"
        }

    Template example:

        workflow = dev=non-prod.yml|test=non-prod.yml|uat=non-prod.yml

    Result:

        {
            "dev": "non-prod.yml",
            "test": "non-prod.yml",
            "uat": "non-prod.yml"
        }

    A single direct workflow is also supported:

        workflow = deploy1.yml

    Result:

        {
            "default": "deploy1.yml"
        }

    For backward compatibility, the property name "workflows"
    is also accepted.
    """

    # Find the preferred workflow property or its backward-compatible name.

    property_name: str | None = None

    for candidate in WORKFLOW_PROPERTY_NAMES:
        if candidate in properties:
            property_name = candidate
            break

    if property_name is None:
        raise ValueError(
            "GitHub custom property 'workflow' is not configured "
            f"for repository '{repository}'. "
            "Expected property name: 'workflow'."
        )

    value = properties[property_name]

    if value is None:
        raise ValueError(
            f"GitHub custom property '{property_name}' "
            f"for repository '{repository}' is empty."
        )

    LOGGER.info(
        "Using GitHub workflow custom property: %s",
        property_name,
    )

    # Handle list values defensively.

    if isinstance(value, list):
        value = "|".join(
            str(item).strip()
            for item in value
            if str(item).strip()
        )

    value = str(value).strip()

    if not value:
        raise ValueError(
            f"GitHub custom property '{property_name}' "
            f"for repository '{repository}' is empty."
        )

    result: dict[str, str] = {}

    # Parse environment-specific workflow mappings.

    if "=" in value:
        mappings = value.split("|")

        for mapping in mappings:
            mapping = mapping.strip()

            if not mapping:
                raise ValueError(
                    f"Empty workflow mapping in custom property '{property_name}' "
                    f"for repository '{repository}'. Remove extra '|' separators."
                )

            if "=" not in mapping:
                raise ValueError(
                    f"Invalid workflow mapping '{mapping}' for repository '{repository}'. "
                    "Expected environment=workflow.yml."
                )

            environment, workflow = mapping.split("=", 1)

            environment = environment.strip().lower()
            workflow = workflow.strip()

            if not environment:
                raise ValueError(
                    f"Workflow mapping '{mapping}' has an empty environment "
                    f"for repository '{repository}'."
                )

            if not workflow:
                raise ValueError(
                    f"Workflow mapping for environment '{environment}' has an empty "
                    f"workflow in repository '{repository}'."
                )

            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", environment):
                raise ValueError(
                    f"Invalid workflow environment '{environment}' for repository "
                    f"'{repository}'. Use lowercase letters, numbers, '_' or '-'."
                )

            if "/" in workflow or "\\" in workflow or workflow in {".", ".."}:
                raise ValueError(
                    f"Invalid workflow filename '{workflow}' for environment "
                    f"'{environment}'. Configure a workflow filename such as 'deploy.yml'."
                )

            if environment in result:
                raise ValueError(
                    f"Duplicate workflow environment '{environment}' is configured "
                    f"for repository '{repository}'."
                )

            result[environment] = workflow

    # Support a single workflow filename for direct deployments.

    else:
        result["default"] = value

    if not result:
        raise ValueError(
            f"No valid deployment workflows configured in custom "
            f"property '{property_name}' for repository "
            f"'{repository}'."
        )

    LOGGER.info(
        "Deployment workflows: %s",
        ", ".join(
            f"{environment}={workflow}"
            for environment, workflow in sorted(result.items())
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

    session.auth = HTTPBasicAuth(
        email,
        token,
    )

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
        LOGGER.exception(
            "%s failed.",
            operation,
        )
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
        LOGGER.error(
            "Jira API response: %s",
            response_text,
        )


def json_object(
    response: Response,
    operation: str,
) -> dict[str, Any]:
    """Return a Jira response as a JSON object."""

    try:
        data = response.json()
    except ValueError as exc:
        raise ValueError(
            f"Invalid JSON returned by {operation}."
        ) from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Unexpected response returned by {operation}."
        )

    return data


def get_jira_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
) -> dict[str, Any]:
    """Retrieve a Jira project."""

    operation = (
        f"Jira project lookup for '{project_key}'"
    )

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
                "Jira authentication failed while looking up project "
                f"'{project_key}'. Check JIRA_EMAIL and JIRA_API_TOKEN."
            )

        if response.status_code == 403:
            raise ValueError(
                f"Jira denied access to project '{project_key}'. "
                "Check the Jira account's project permissions."
            )

        if response.status_code == 404:
            raise ValueError(
                f"Jira project '{project_key}' was not found. "
                "Check the jira_projects custom property and Jira project key."
            )

        if response.status_code == 429:
            raise ValueError(
                "Jira API rate limit exceeded. Wait and rerun the workflow."
            )

        if response.status_code >= 500:
            raise ValueError(
                f"Jira API is temporarily unavailable (HTTP {response.status_code}). "
                "Retry the workflow later."
            )

        response.raise_for_status()

    return json_object(
        response,
        operation,
    )


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
    """
    Create a Jira release/version.

    Jira Cloud endpoint:

        POST /rest/api/3/version
    """

    operation = (
        f"Jira release creation for '{project_key}'"
    )

    url = f"{jira_url}/rest/api/3/version"

    LOGGER.info(
        "Jira release create endpoint: POST %s",
        url,
    )

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
            json_object(
                response,
                operation,
            ),
        )

    if is_duplicate_version(response):
        LOGGER.warning(
            "Release '%s' already exists in Jira project '%s'. "
            "Skipping this project and continuing.",
            version,
            project_key,
        )

        return "exists", None

    log_api_error(response, operation)

    if response.status_code == 401:
        raise ValueError(
            f"Jira authentication failed while creating release '{version}' "
            f"in project '{project_key}'. Check JIRA_EMAIL and JIRA_API_TOKEN."
        )

    if response.status_code == 403:
        raise ValueError(
            f"Jira denied permission to create release '{version}' "
            f"in project '{project_key}'. Check project permissions."
        )

    if response.status_code == 404:
        raise ValueError(
            f"Jira endpoint or project '{project_key}' was not found while "
            f"creating release '{version}'. Verify the Jira URL and project access."
        )

    if response.status_code == 429:
        raise ValueError(
            "Jira API rate limit exceeded while creating a release. Retry later."
        )

    if response.status_code >= 500:
        raise ValueError(
            f"Jira API is temporarily unavailable (HTTP {response.status_code}) "
            "while creating a release. Retry later."
        )

    response.raise_for_status()

    raise RuntimeError(
        "Unexpected Jira API response."
    )


# Jira release metadata serialization.

def build_release_description(
    environment: dict[str, str],
    deployment_type: str,
    workflows: dict[str, str],
) -> str:
    """
    Build the Jira release description.

    Format:

        version=VERSION|tag=TAG|repository=REPOSITORY|
        deploymentType=TYPE|workflows=ENV:WORKFLOW,...|ref=REF

    Example - direct:

        version=1.0.0|tag=v1.0.0|
        repository=Test-Organization7144/TestGithubWorkflow|
        deploymentType=direct|
        workflows=prod:proddeploy.yml,uat:uatdeploy.yml|
        ref=main

    Example - template:

        version=1.0.0|tag=v1.0.0|
        repository=Test-Organization7144/Test-Deployment-App|
        deploymentType=template|
        workflows=dev:non-prod.yml,test:non-prod.yml,uat:non-prod.yml|
        ref=main
    """

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

    LOGGER.info(
        "========================================"
    )

    LOGGER.info(
        "Creating release in Jira project: %s",
        project_key,
    )

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
            "a project ID."
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
        "Release created successfully | "
        "project: %s | "
        "name: %s | "
        "ID: %s",
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

    LOGGER.info(
        "========================================"
    )

    LOGGER.info(
        "Release configuration"
    )

    LOGGER.info(
        "========================================"
    )

    LOGGER.info(
        "Repository       : %s",
        environment["REPOSITORY"],
    )

    LOGGER.info(
        "Version          : %s",
        environment["VERSION"],
    )

    LOGGER.info(
        "Tag              : %s",
        environment["TAG"],
    )

    LOGGER.info(
        "REF              : %s",
        environment["REF"],
    )

    LOGGER.info(
        "Deployment Type  : %s",
        deployment_type,
    )

    LOGGER.info(
        "Jira             : %s",
        environment["JIRA_URL"],
    )

    LOGGER.info(
        "Projects         : %s",
        ", ".join(projects),
    )

    LOGGER.info(
        "Workflows        : %s",
        workflow_text,
    )

    LOGGER.info(
        "Description      : %s",
        description,
    )

    LOGGER.info(
        "========================================"
    )


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

        LOGGER.info(
            "Starting Jira release creation."
        )

        # Initialize authenticated GitHub API access using the supplied token.
        # In GitHub Actions, GITHUB_TOKEN should contain the generated App
        # installation token, not a Personal Access Token.

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

        LOGGER.info(
            "========================================"
        )

        LOGGER.info(
            "Release processing completed"
        )

        LOGGER.info(
            "Created       : %s",
            ", ".join(created) or "None",
        )

        LOGGER.info(
            "Already exists: %s",
            ", ".join(existing) or "None",
        )

        LOGGER.info(
            "Deployment Type: %s",
            deployment_type,
        )

        LOGGER.info(
            "========================================"
        )

        return 0

    except ValueError as exc:
        LOGGER.error(
            "%s",
            exc,
        )

    except requests.HTTPError as exc:
        # Keep HTTP failures actionable without logging credentials or headers.
        status_code = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        LOGGER.error(
            "API request failed with HTTP status %s: %s",
            status_code,
            exc,
        )

    except requests.Timeout:
        LOGGER.error(
            "An API request timed out after %s seconds. Check connectivity and retry.",
            REQUEST_TIMEOUT,
        )

    except requests.ConnectionError as exc:
        LOGGER.error(
            "Could not connect to GitHub or Jira API: %s",
            exc,
        )

    except requests.RequestException as exc:
        LOGGER.error(
            "API request failed: %s",
            exc,
        )

    except KeyboardInterrupt:
        LOGGER.error(
            "Process interrupted by user."
        )

        return 130

    except Exception:
        LOGGER.exception(
            "Unexpected error while creating Jira releases."
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
