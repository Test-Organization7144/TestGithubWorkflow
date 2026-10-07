"""Create Jira releases using GitHub repository custom properties."""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import requests
from requests import Response
from requests.auth import HTTPBasicAuth


REQUEST_TIMEOUT = 30

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"

JIRA_PROJECTS_PROPERTY = "jira_projects"

# Current GitHub Custom Property name:
#     workflow
#
# "workflows" is also supported for backward compatibility.
WORKFLOW_PROPERTY_NAMES = (
    "workflow",
    "workflows",
)

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def required_env(name: str) -> str:
    """Return a required environment variable."""

    value = os.getenv(name, "").strip()

    if not value:
        raise ValueError(
            f"Required environment variable '{name}' is missing or empty."
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

    return environment


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

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

    parts = repository.split("/", 1)

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
        f"/repos/{owner}/{repo}/properties/values"
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
                "GitHub authentication failed while reading "
                "repository custom properties. "
                "Check GITHUB_TOKEN."
            )

        if response.status_code == 403:
            raise ValueError(
                "GitHub denied access to repository custom properties. "
                "Check the GITHUB_TOKEN permissions and organization "
                "repository access."
            )

        if response.status_code == 404:
            raise ValueError(
                f"GitHub repository '{repository}' was not found "
                "or the token cannot access it."
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


# ---------------------------------------------------------------------------
# Jira project configuration from GitHub custom properties
# ---------------------------------------------------------------------------

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

    LOGGER.info(
        "Jira projects: %s",
        ", ".join(projects),
    )

    return projects


# ---------------------------------------------------------------------------
# Workflow configuration from GitHub custom properties
# ---------------------------------------------------------------------------

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

    A single direct workflow is also supported:

        workflow = deploy1.yml

    Result:

        {
            "default": "deploy1.yml"
        }

    For backward compatibility, the property name "workflows"
    is also accepted.
    """

    # ---------------------------------------------------------------
    # Find the workflow property.
    #
    # Preferred/current name:
    #
    #     workflow
    #
    # Backward-compatible name:
    #
    #     workflows
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # Handle list values defensively.
    #
    # Example:
    #
    # [
    #     "uat=uatdeploy.yml",
    #     "prod=proddeploy.yml"
    # ]
    # ---------------------------------------------------------------

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

    # ---------------------------------------------------------------
    # Environment-specific format:
    #
    # uat=uatdeploy.yml|prod=proddeploy.yml
    # ---------------------------------------------------------------

    if "=" in value:

        mappings = value.split("|")

        for mapping in mappings:

            mapping = mapping.strip()

            if not mapping:
                continue

            if "=" not in mapping:
                LOGGER.warning(
                    "Ignoring invalid workflow mapping '%s' "
                    "for repository '%s'.",
                    mapping,
                    repository,
                )
                continue

            environment, workflow = mapping.split(
                "=",
                1,
            )

            environment = environment.strip().lower()
            workflow = workflow.strip()

            if not environment:
                LOGGER.warning(
                    "Ignoring workflow mapping with empty "
                    "environment for repository '%s'.",
                    repository,
                )
                continue

            if not workflow:
                LOGGER.warning(
                    "Ignoring workflow mapping with empty workflow "
                    "for environment '%s' in repository '%s'.",
                    environment,
                    repository,
                )
                continue

            result[environment] = workflow

    # ---------------------------------------------------------------
    # Single workflow format:
    #
    # deploy1.yml
    # ---------------------------------------------------------------

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
            for environment, workflow
            in sorted(result.items())
        ),
    )

    return result


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

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
        f"{jira_url}/rest/api/3/project/{project_key}",
        operation,
    )

    if not response.ok:
        log_api_error(
            response,
            operation,
        )
        response.raise_for_status()

    return json_object(
        response,
        operation,
    )


# ---------------------------------------------------------------------------
# Jira version / release
# ---------------------------------------------------------------------------

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

    log_api_error(
        response,
        operation,
    )

    response.raise_for_status()

    raise RuntimeError(
        "Unexpected Jira API response."
    )


# ---------------------------------------------------------------------------
# Jira release description
# ---------------------------------------------------------------------------

def build_release_description(
    environment: dict[str, str],
    workflows: dict[str, str],
) -> str:
    """
    Build the Jira release description.

    Format:

        version=VERSION|tag=TAG|repository=REPOSITORY|workflows=ENV:WORKFLOW,...|ref=REF

    Example:

        version=1.0.0|tag=v1.0.0|repository=Test-Organization7144/TestGithubWorkflow|workflows=prod:proddeploy.yml,uat:uatdeploy.yml|ref=main
    """

    workflow_text = ",".join(
        f"{environment_name}:{workflow}"
        for environment_name, workflow
        in sorted(workflows.items())
    )

    return (
        f"version={environment['VERSION']}"
        f"|tag={environment['TAG']}"
        f"|repository={environment['REPOSITORY']}"
        f"|workflows={workflow_text}"
        f"|ref={environment['REF']}"
    )


# ---------------------------------------------------------------------------
# Process Jira project
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_configuration(
    environment: dict[str, str],
    projects: list[str],
    workflows: dict[str, str],
    description: str,
) -> None:
    """Log release configuration."""

    workflow_text = ", ".join(
        f"{environment_name}={workflow}"
        for environment_name, workflow
        in sorted(workflows.items())
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
        "Repository : %s",
        environment["REPOSITORY"],
    )

    LOGGER.info(
        "Version    : %s",
        environment["VERSION"],
    )

    LOGGER.info(
        "Tag        : %s",
        environment["TAG"],
    )

    LOGGER.info(
        "REF        : %s",
        environment["REF"],
    )

    LOGGER.info(
        "Jira       : %s",
        environment["JIRA_URL"],
    )

    LOGGER.info(
        "Projects   : %s",
        ", ".join(projects),
    )

    LOGGER.info(
        "Workflows  : %s",
        workflow_text,
    )

    LOGGER.info(
        "Description: %s",
        description,
    )

    LOGGER.info(
        "========================================"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Run the Jira release creation process."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:

        # ---------------------------------------------------------------
        # 1. Load GitHub Actions environment
        # ---------------------------------------------------------------

        environment = load_environment()

        LOGGER.info(
            "Starting Jira release creation."
        )

        # ---------------------------------------------------------------
        # 2. Create GitHub API session
        # ---------------------------------------------------------------

        github_session = create_github_session(
            environment["GITHUB_TOKEN"],
        )

        # ---------------------------------------------------------------
        # 3. Read GitHub custom properties
        # ---------------------------------------------------------------

        properties = get_repository_properties(
            github_session,
            environment["REPOSITORY"],
        )

        # ---------------------------------------------------------------
        # 4. Read Jira projects from custom property
        # ---------------------------------------------------------------

        projects = get_jira_projects(
            properties,
            environment["REPOSITORY"],
        )

        # ---------------------------------------------------------------
        # 5. Read workflows from custom property
        # ---------------------------------------------------------------

        workflows = get_workflows(
            properties,
            environment["REPOSITORY"],
        )

        # ---------------------------------------------------------------
        # 6. Build Jira release description
        # ---------------------------------------------------------------

        description = build_release_description(
            environment,
            workflows,
        )

        # ---------------------------------------------------------------
        # 7. Log configuration
        # ---------------------------------------------------------------

        log_configuration(
            environment,
            projects,
            workflows,
            description,
        )

        # ---------------------------------------------------------------
        # 8. Create Jira API session
        # ---------------------------------------------------------------

        jira_session = create_jira_session(
            environment["JIRA_EMAIL"],
            environment["JIRA_API_TOKEN"],
        )

        created: list[str] = []
        existing: list[str] = []

        # ---------------------------------------------------------------
        # 9. Create release in every configured Jira project
        # ---------------------------------------------------------------

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

        # ---------------------------------------------------------------
        # 10. Final result
        # ---------------------------------------------------------------

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
            "========================================"
        )

        return 0

    except ValueError as exc:

        LOGGER.error(
            "%s",
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
