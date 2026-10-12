"""Create Jira releases using GitHub repository custom properties."""

from __future__ import annotations

import logging
import os
import re
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

# Deployment model and supported repository types.
#
# Supported values:
#
#     direct
#     template
#
# The property is optional for backward compatibility.
# If it is missing or empty, the repository is treated as direct.
DEPLOYMENT_TYPE_PROPERTY = "deployment_type"

DEPLOYMENT_TYPE_DIRECT = "direct"
DEPLOYMENT_TYPE_TEMPLATE = "template"

SUPPORTED_DEPLOYMENT_TYPES = {
    DEPLOYMENT_TYPE_DIRECT,
    DEPLOYMENT_TYPE_TEMPLATE,
}

# GitHub workflow custom-property names.
#
# Current property: workflow
# Backward-compatible property: workflows
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
            f"Required environment variable '{name}' is missing or empty. "
            "Check the workflow inputs, repository variables, and secrets."
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

    environment = {
        name: required_env(name)
        for name in names
    }

    environment["JIRA_URL"] = environment["JIRA_URL"].rstrip("/")

    validate_environment(environment)

    return environment


def validate_environment(environment: dict[str, str]) -> None:
    """Validate release inputs before calling GitHub or Jira APIs."""

    version = environment["VERSION"]
    tag = environment["TAG"]
    repository = environment["REPOSITORY"]
    ref = environment["REF"]
    jira_url = environment["JIRA_URL"]

    # Validate the Jira release version without restricting normal versioning.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ValueError(
            f"Invalid VERSION '{version}'. "
            "Use letters, numbers, dots, underscores, plus signs, or hyphens. "
            "The version must begin with a letter or number."
        )

    # Reject invalid tag inputs before processing release metadata.
    if (
        not tag
        or tag.startswith("-")
        or any(character.isspace() for character in tag)
        or any(character in tag for character in ("~", "^", ":", "?", "*", "[" , "\\")):
        raise ValueError(
            f"Invalid TAG '{tag}'. Provide a valid Git tag name."
        )

    if tag.endswith(".") or tag.endswith(".lock"):
        raise ValueError(
            f"Invalid TAG '{tag}'. Git tags cannot end with a dot "
            "or '.lock'."
        )

    if ".." in tag or "@{" in tag or "//" in tag:
        raise ValueError(
            f"Invalid TAG '{tag}'. Check the Git tag naming rules."
        )

    # Validate the source reference input.
    if (
        not ref
        or ref.startswith("-")
        or any(character.isspace() for character in ref)
        or any(character in ref for character in ("~", "^", ":", "?", "*", "[", "\\"))
    ):
        raise ValueError(
            f"Invalid REF '{ref}'. Provide a valid branch name or tag name."
        )

    if ref.endswith(".") or ref.endswith(".lock"):
        raise ValueError(
            f"Invalid REF '{ref}'. Git refs cannot end with a dot "
            "or '.lock'."
        )

    if ".." in ref or "@{" in ref or "//" in ref:
        raise ValueError(
            f"Invalid REF '{ref}'. Check the Git ref naming rules."
        )

    # Validate the owner/repository format.
    parse_repository(repository)

    # Validate the Jira URL format.
    if not jira_url.startswith(("https://", "http://")):
        raise ValueError(
            f"Invalid JIRA_URL '{jira_url}'. "
            "Provide a valid Jira site URL beginning with https://."
        )

    if any(character.isspace() for character in jira_url):
        raise ValueError(
            "JIRA_URL cannot contain whitespace."
        )


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
            "GitHub repository cannot be empty. "
            "Set REPOSITORY to the format 'owner/repository'."
        )

    parts = repository.split("/")

    if len(parts) != 2:
        raise ValueError(
            f"Invalid GitHub repository '{repository}'. "
            "Expected format: owner/repository, for example "
            "'Test-Organization7144/TestGithubWorkflow'."
        )

    owner = parts[0].strip()
    repo = parts[1].strip()

    if (
        not owner
        or not repo
        or owner in {".", ".."}
        or repo in {".", ".."}
        or any(character.isspace() for character in owner)
        or any(character.isspace() for character in repo)
    ):
        raise ValueError(
            f"Invalid GitHub repository '{repository}'. "
            "The owner and repository name must both be valid and non-empty."
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
    """Execute a GitHub API request with timeout and connection handling."""

    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )

    except requests.Timeout as exc:
        LOGGER.error(
            "%s timed out after %s seconds.",
            operation,
            REQUEST_TIMEOUT,
        )
        raise RuntimeError(
            f"{operation} timed out. Check GitHub API availability "
            "and retry the workflow."
        ) from exc

    except requests.RequestException as exc:
        LOGGER.exception(
            "%s failed because of a GitHub connection error.",
            operation,
        )
        raise RuntimeError(
            f"{operation} failed because of a GitHub connection error: {exc}"
        ) from exc

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

    try:
        data = response.json()
    except ValueError:
        data = {}

    if isinstance(data, dict) and data.get("message"):
        LOGGER.error(
            "GitHub API message: %s",
            data["message"],
        )

    response_text = response.text.strip()

    if response_text and not isinstance(data, dict):
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
            f"Invalid JSON returned by {operation}. "
            "Check the GitHub API response and repository permissions."
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
        f"/repos/{quote(owner, safe='')}"
        f"/{quote(repo, safe='')}/properties/values"
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
        log_github_api_error(
            response,
            operation,
        )

        if response.status_code == 401:
            raise ValueError(
                "GitHub authentication failed while reading repository "
                "custom properties. Check GITHUB_TOKEN and ensure it has "
                "not expired or been revoked."
            )

        if response.status_code == 403:
            raise ValueError(
                "GitHub denied access to repository custom properties. "
                "Check token permissions, organization access, repository "
                "access, and any organization SSO or policy requirements."
            )

        if response.status_code == 404:
            raise ValueError(
                f"GitHub repository '{repository}' was not found, or the "
                "token cannot access it. Verify the repository owner/name "
                "and token permissions."
            )

        if response.status_code == 422:
            raise ValueError(
                f"GitHub rejected the custom-property request for "
                f"'{repository}'. Verify the endpoint and repository "
                "configuration."
            )

        response.raise_for_status()

    data = json_value(
        response,
        operation,
    )

    if not isinstance(data, list):
        raise ValueError(
            "GitHub repository properties response must be a list. "
            "The GitHub API returned an unexpected response format."
        )

    properties: dict[str, Any] = {}

    for item in data:
        if not isinstance(item, dict):
            LOGGER.warning(
                "Ignoring an unexpected entry in repository properties."
            )
            continue

        property_name = item.get("property_name")

        if not isinstance(property_name, str) or not property_name.strip():
            LOGGER.warning(
                "Ignoring a repository custom property with no valid name."
            )
            continue

        # Normalize names so property lookup is case-insensitive.
        properties[property_name.strip().lower()] = item.get("value")

    LOGGER.info(
        "Custom properties found: %s",
        ", ".join(sorted(properties.keys())) or "None",
    )

    if not properties:
        raise ValueError(
            f"No readable GitHub custom properties were returned for "
            f"repository '{repository}'. Configure at least "
            f"'{JIRA_PROJECTS_PROPERTY}' and one workflow property "
            f"('{WORKFLOW_PROPERTY_NAMES[0]}' or "
            f"'{WORKFLOW_PROPERTY_NAMES[1]}')."
        )

    return properties


def get_property_value(
    properties: dict[str, Any],
    property_name: str,
    repository: str,
) -> Any:
    """Return a required GitHub custom property."""

    normalized_name = property_name.strip().lower()

    if normalized_name not in properties:
        raise ValueError(
            f"Required GitHub custom property '{property_name}' is not "
            f"configured for repository '{repository}'. "
            f"Add the property in GitHub repository settings and retry."
        )

    value = properties[normalized_name]

    if value is None:
        raise ValueError(
            f"GitHub custom property '{property_name}' for repository "
            f"'{repository}' is empty. Set a valid value in GitHub "
            "repository settings."
        )

    if isinstance(value, str) and not value.strip():
        raise ValueError(
            f"GitHub custom property '{property_name}' for repository "
            f"'{repository}' is empty. Set a valid value in GitHub "
            "repository settings."
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

    The property is optional. If missing or empty, default to direct.
    """

    property_name = DEPLOYMENT_TYPE_PROPERTY.lower()

    # Existing repositories remain compatible without this property.
    if property_name not in properties:
        LOGGER.info(
            "Custom property '%s' is not configured for repository '%s'. "
            "Defaulting deployment type to '%s'.",
            DEPLOYMENT_TYPE_PROPERTY,
            repository,
            DEPLOYMENT_TYPE_DIRECT,
        )
        return DEPLOYMENT_TYPE_DIRECT

    value = properties[property_name]

    if value is None or value == "":
        LOGGER.info(
            "Custom property '%s' is empty for repository '%s'. "
            "Defaulting deployment type to '%s'.",
            DEPLOYMENT_TYPE_PROPERTY,
            repository,
            DEPLOYMENT_TYPE_DIRECT,
        )
        return DEPLOYMENT_TYPE_DIRECT

    # GitHub custom properties may be returned as strings or lists.
    if isinstance(value, list):
        if not value:
            LOGGER.info(
                "Deployment type property is an empty list for repository "
                "'%s'. Defaulting to '%s'.",
                repository,
                DEPLOYMENT_TYPE_DIRECT,
            )
            return DEPLOYMENT_TYPE_DIRECT

        if len(value) > 1:
            raise ValueError(
                f"Invalid '{DEPLOYMENT_TYPE_PROPERTY}' property for "
                f"repository '{repository}'. Configure exactly one value: "
                "'direct' or 'template'."
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
            f"Invalid GitHub custom property "
            f"'{DEPLOYMENT_TYPE_PROPERTY}'='{deployment_type}' for "
            f"repository '{repository}'. Supported values are: "
            f"{', '.join(sorted(SUPPORTED_DEPLOYMENT_TYPES))}. "
            "Correct the value in GitHub repository settings."
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
        raw_projects = [
            str(project).strip()
            for project in value
            if str(project).strip()
        ]
    elif isinstance(value, str):
        # Support commas, semicolons, pipes, and new lines.
        raw_projects = [
            project.strip()
            for project in re.split(r"[,;|\n]+", value)
            if project.strip()
        ]
    else:
        raise ValueError(
            f"Invalid GitHub custom property '{JIRA_PROJECTS_PROPERTY}' "
            f"for repository '{repository}'. Expected a string or a list "
            "of Jira project keys, for example 'APPDEPLOY,SCRUM'."
        )

    if not raw_projects:
        raise ValueError(
            f"No Jira projects are configured in custom property "
            f"'{JIRA_PROJECTS_PROPERTY}' for repository '{repository}'. "
            "Configure one or more Jira project keys."
        )

    projects: list[str] = []
    seen: set[str] = set()

    for project in raw_projects:
        project_key = project.upper()

        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project_key):
            raise ValueError(
                f"Invalid Jira project key '{project}' in custom property "
                f"'{JIRA_PROJECTS_PROPERTY}' for repository '{repository}'. "
                "Project keys must begin with a letter and contain only "
                "letters, numbers, or underscores."
            )

        if project_key not in seen:
            projects.append(project_key)
            seen.add(project_key)

    LOGGER.info(
        "Jira projects: %s",
        ", ".join(projects),
    )

    return projects


# Workflow configuration from repository properties.

def validate_workflow_filename(
    workflow: str,
    repository: str,
    property_name: str,
    environment: str,
) -> str:
    """Validate a workflow filename before storing it in release metadata."""

    workflow = workflow.strip()

    if not workflow:
        raise ValueError(
            f"Workflow filename is empty for environment '{environment}' "
            f"in property '{property_name}' for repository '{repository}'."
        )

    if (
        "/" in workflow
        or "\\" in workflow
        or workflow in {".", ".."}
        or workflow.startswith(".")
        or any(character.isspace() for character in workflow)
        or not workflow.lower().endswith((".yml", ".yaml"))
    ):
        raise ValueError(
            f"Invalid workflow filename '{workflow}' for environment "
            f"'{environment}' in property '{property_name}' for repository "
            f"'{repository}'. Provide a workflow filename such as "
            "'uatdeploy.yml' or 'non-prod.yml', without a directory path."
        )

    return workflow


def get_workflows(
    properties: dict[str, Any],
    repository: str,
) -> dict[str, str]:
    """
    Read deployment workflows from the GitHub custom property.

    Supported environment mapping:
        uat=uatdeploy.yml|prod=proddeploy.yml

    Template example:
        dev=non-prod.yml|test=non-prod.yml|uat=non-prod.yml

    Single workflow example:
        deploy1.yml

    The property name 'workflows' is accepted for backward compatibility.
    """

    # Find the preferred property name, ignoring property-name case.
    property_name: str | None = None

    for candidate in WORKFLOW_PROPERTY_NAMES:
        normalized_candidate = candidate.lower()

        if normalized_candidate in properties:
            property_name = normalized_candidate
            break

    if property_name is None:
        raise ValueError(
            f"Required GitHub custom property '{WORKFLOW_PROPERTY_NAMES[0]}' "
            f"is not configured for repository '{repository}'. "
            f"For backward compatibility, '{WORKFLOW_PROPERTY_NAMES[1]}' "
            "is also accepted. Example value: "
            "'uat=uatdeploy.yml|prod=proddeploy.yml'."
        )

    value = properties[property_name]

    if value is None:
        raise ValueError(
            f"GitHub custom property '{property_name}' for repository "
            f"'{repository}' is empty. Configure valid workflow mappings."
        )

    LOGGER.info(
        "Using GitHub workflow custom property: %s",
        property_name,
    )

    # Support GitHub properties represented as a list.
    if isinstance(value, list):
        if not value:
            raise ValueError(
                f"GitHub custom property '{property_name}' for repository "
                f"'{repository}' is empty. Configure valid workflow mappings."
            )

        if not all(isinstance(item, str) for item in value):
            raise ValueError(
                f"Invalid GitHub custom property '{property_name}' for "
                f"repository '{repository}'. Every workflow mapping must "
                "be a string."
            )

        value = "|".join(
            item.strip()
            for item in value
            if item.strip()
        )

    elif not isinstance(value, str):
        raise ValueError(
            f"Invalid GitHub custom property '{property_name}' for "
            f"repository '{repository}'. Expected a string or a list "
            "of strings."
        )

    value = value.strip()

    if not value:
        raise ValueError(
            f"GitHub custom property '{property_name}' for repository "
            f"'{repository}' is empty. Configure valid workflow mappings."
        )

    result: dict[str, str] = {}

    # Support a single workflow filename for backward compatibility.
    if "=" not in value and ":" not in value:
        result["default"] = validate_workflow_filename(
            value,
            repository,
            property_name,
            "default",
        )

        LOGGER.info(
            "Single deployment workflow: %s",
            result["default"],
        )

        return result

    # Parse environment-specific workflow mappings.
    mappings = re.split(r"[|,\n]+", value)

    for mapping in mappings:
        mapping = mapping.strip()

        if not mapping:
            continue

        if "=" in mapping:
            environment, workflow = mapping.split("=", 1)
        elif ":" in mapping:
            environment, workflow = mapping.split(":", 1)
        else:
            raise ValueError(
                f"Invalid workflow mapping '{mapping}' in property "
                f"'{property_name}' for repository '{repository}'. "
                "Use 'environment=workflow.yml', for example "
                "'uat=uatdeploy.yml'."
            )

        environment = environment.strip().lower()
        workflow = workflow.strip()

        if not environment:
            raise ValueError(
                f"Workflow mapping '{mapping}' has an empty environment "
                f"in property '{property_name}' for repository "
                f"'{repository}'."
            )

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", environment):
            raise ValueError(
                f"Invalid environment name '{environment}' in property "
                f"'{property_name}' for repository '{repository}'. "
                "Use letters, numbers, hyphens, or underscores."
            )

        if environment in result:
            raise ValueError(
                f"Duplicate workflow mapping for environment '{environment}' "
                f"in property '{property_name}' for repository "
                f"'{repository}'. Configure each environment only once."
            )

        result[environment] = validate_workflow_filename(
            workflow,
            repository,
            property_name,
            environment,
        )

    if not result:
        raise ValueError(
            f"No valid deployment workflows are configured in property "
            f"'{property_name}' for repository '{repository}'. "
            "Example: 'uat=uatdeploy.yml|prod=proddeploy.yml'."
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
    """Execute a Jira request with timeout and connection handling."""

    try:
        response = session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )

    except requests.Timeout as exc:
        LOGGER.error(
            "%s timed out after %s seconds.",
            operation,
            REQUEST_TIMEOUT,
        )
        raise RuntimeError(
            f"{operation} timed out. Check Jira availability and retry."
        ) from exc

    except requests.RequestException as exc:
        LOGGER.exception(
            "%s failed because of a Jira connection error.",
            operation,
        )
        raise RuntimeError(
            f"{operation} failed because of a Jira connection error: {exc}"
        ) from exc

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

    try:
        data = response.json()
    except ValueError:
        data = {}

    if isinstance(data, dict):
        messages = data.get("errorMessages", [])
        field_errors = data.get("errors", {})

        if messages:
            LOGGER.error(
                "Jira API error messages: %s",
                "; ".join(str(message) for message in messages),
            )

        if field_errors:
            LOGGER.error(
                "Jira API field errors: %s",
                field_errors,
            )

    response_text = response.text.strip()

    if response_text and not isinstance(data, dict):
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
            f"Unexpected response returned by {operation}. "
            "Expected a JSON object."
        )

    return data


def get_jira_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
) -> dict[str, Any]:
    """Retrieve and validate a Jira project."""

    operation = (
        f"Jira project lookup for '{project_key}'"
    )

    encoded_project_key = quote(project_key, safe="")

    response = jira_request(
        session,
        "GET",
        f"{jira_url}/rest/api/3/project/{encoded_project_key}",
        operation,
    )

    if not response.ok:
        log_api_error(
            response,
            operation,
        )

        if response.status_code == 401:
            raise ValueError(
                "Jira authentication failed while looking up project "
                f"'{project_key}'. Verify JIRA_EMAIL and JIRA_API_TOKEN."
            )

        if response.status_code == 403:
            raise ValueError(
                f"Jira denied access to project '{project_key}'. "
                "Verify that the Jira account has permission to browse "
                "the project and create versions."
            )

        if response.status_code == 404:
            raise ValueError(
                f"Jira project '{project_key}' was not found or is not "
                "accessible to the configured Jira account. Verify the "
                "project key and Jira permissions."
            )

        response.raise_for_status()

    project = json_object(
        response,
        operation,
    )

    if not project.get("id"):
        raise ValueError(
            f"Jira project '{project_key}' response does not contain "
            "a project ID."
        )

    if not project.get("key"):
        LOGGER.warning(
            "Jira project response for '%s' did not include a project key.",
            project_key,
        )

    return project


# Jira release creation and duplicate handling.

def is_duplicate_version(
    response: Response,
) -> bool:
    """Return whether a Jira response indicates an existing version."""

    if response.status_code != 400:
        return False

    try:
        data = response.json()
    except ValueError:
        data = {}

    response_text = response.text.lower()

    indicators = (
        "already exists",
        "version already exists",
        "a version with this name already exists",
        "name already exists",
        "version name must be unique",
    )

    if any(indicator in response_text for indicator in indicators):
        return True

    if isinstance(data, dict):
        messages = data.get("errorMessages", [])

        if isinstance(messages, list):
            combined_messages = " ".join(
                str(message).lower()
                for message in messages
            )

            return any(
                indicator in combined_messages
                for indicator in indicators
            )

    return False


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
            "archived": False,
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

    log_api_error(
        response,
        operation,
    )

    if response.status_code == 401:
        raise ValueError(
            "Jira authentication failed while creating release "
            f"'{version}' in project '{project_key}'. "
            "Verify JIRA_EMAIL and JIRA_API_TOKEN."
        )

    if response.status_code == 403:
        raise ValueError(
            f"Jira denied permission to create release '{version}' "
            f"in project '{project_key}'. Verify the account's project "
            "administration permissions."
        )

    if response.status_code == 404:
        raise ValueError(
            f"Jira could not find the endpoint or project while creating "
            f"release '{version}' in project '{project_key}'. Verify "
            "JIRA_URL and the project ID."
        )

    if response.status_code == 400:
        raise ValueError(
            f"Jira rejected release '{version}' in project '{project_key}'. "
            "Check the release name, project ID, and description format."
        )

    response.raise_for_status()

    raise RuntimeError(
        f"Unexpected Jira API response while creating release '{version}' "
        f"in project '{project_key}'."
    )


# Jira release metadata serialization.

def build_release_description(
    environment: dict[str, str],
    deployment_type: str,
    workflows: dict[str, str],
) -> str:
    """
    Build the Jira release description consumed by the Forge app.

    Format:
        version=VERSION|tag=TAG|repository=REPOSITORY|
        deploymentType=TYPE|workflows=ENV:WORKFLOW,...|ref=REF

    The ref field remains last for compatibility with the existing parser.
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

    LOGGER.info("========================================")
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
    """Log release configuration without exposing credentials."""

    workflow_text = ", ".join(
        f"{environment_name}={workflow}"
        for environment_name, workflow in sorted(workflows.items())
    )

    LOGGER.info("========================================")
    LOGGER.info("Release configuration")
    LOGGER.info("========================================")

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

    LOGGER.info("========================================")


# Application entry point and orchestration.

def main() -> int:
    """Run the Jira release creation process."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stdout,
    )

    try:
        # Load and validate workflow inputs and credentials.
        environment = load_environment()

        LOGGER.info(
            "Starting Jira release creation."
        )

        # Initialize authenticated GitHub API access.
        github_session = create_github_session(
            environment["GITHUB_TOKEN"],
        )

        # Load repository-level deployment configuration.
        properties = get_repository_properties(
            github_session,
            environment["REPOSITORY"],
        )

        # Resolve direct or template deployment mode.
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
        failed: list[str] = []

        # Process all configured Jira projects independently.
        for project in projects:
            try:
                status = process_project(
                    session=jira_session,
                    jira_url=environment["JIRA_URL"],
                    project_key=project,
                    version=environment["VERSION"],
                    description=description,
                )

                if status == "created":
                    created.append(project)
                elif status == "exists":
                    existing.append(project)
                else:
                    failed.append(project)
                    LOGGER.error(
                        "Unexpected processing status '%s' for project '%s'.",
                        status,
                        project,
                    )

            except (
                ValueError,
                RuntimeError,
                requests.RequestException,
            ) as exc:
                failed.append(project)
                LOGGER.error(
                    "Failed to process Jira project '%s': %s",
                    project,
                    exc,
                )

        # Report created, existing, and failed releases.
        LOGGER.info("========================================")
        LOGGER.info("Release processing completed")
        LOGGER.info(
            "Created        : %s",
            ", ".join(created) or "None",
        )
        LOGGER.info(
            "Already exists : %s",
            ", ".join(existing) or "None",
        )
        LOGGER.info(
            "Failed         : %s",
            ", ".join(failed) or "None",
        )
        LOGGER.info(
            "Deployment Type: %s",
            deployment_type,
        )
        LOGGER.info("========================================")

        if failed:
            LOGGER.error(
                "Release creation failed for project(s): %s.",
                ", ".join(failed),
            )
            return 1

        if not created and not existing:
            LOGGER.error(
                "No releases were created or found to already exist. "
                "Review the repository configuration and API responses."
            )
            return 1

        LOGGER.info(
            "Jira release creation completed successfully."
        )

        return 0

    except ValueError as exc:
        LOGGER.error(
            "Configuration or validation error: %s",
            exc,
        )

    except RuntimeError as exc:
        LOGGER.error(
            "Operation failed: %s",
            exc,
        )

    except requests.HTTPError as exc:
        LOGGER.error(
            "API request failed: %s",
            exc,
        )

    except requests.RequestException as exc:
        LOGGER.error(
            "API connection failed: %s",
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
