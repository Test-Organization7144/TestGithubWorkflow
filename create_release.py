"""Create Jira releases for configured GitHub repositories."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import requests
import yaml
from requests import Response
from requests.auth import HTTPBasicAuth

CONFIG_FILE = Path("jira-projects.yml")
REQUEST_TIMEOUT = 30
REPOSITORIES_KEY = "repositories"

LOGGER = logging.getLogger(__name__)


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
        "ENVIRONMENT",
        "REPOSITORY",
        "REF",
        "JIRA_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
    )

    environment = {
        name: required_env(name)
        for name in names
    }

    environment["JIRA_URL"] = environment["JIRA_URL"].rstrip("/")

    return environment


def load_configuration(path: Path) -> dict[str, Any]:
    """Load and validate the Jira project configuration."""
    if not path.is_file():
        raise FileNotFoundError(
            f"Configuration file '{path}' was not found."
        )

    with path.open(encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(
            f"Configuration file '{path}' must contain a YAML mapping."
        )

    repositories = config.get(REPOSITORIES_KEY)

    if not isinstance(repositories, dict):
        raise ValueError(
            f"'{REPOSITORIES_KEY}' must be a mapping in '{path}'."
        )

    return config


def get_repository_config(
    config: dict[str, Any],
    repository: str,
) -> dict[str, Any]:
    """Return configuration for a GitHub repository."""
    repository_config = config[REPOSITORIES_KEY].get(repository)

    if repository_config is None:
        raise ValueError(
            f"Repository '{repository}' is not configured."
        )

    if not isinstance(repository_config, dict):
        raise ValueError(
            f"Configuration for repository '{repository}' must be a mapping."
        )

    return repository_config


def get_jira_projects(
    config: dict[str, Any],
    repository: str,
) -> list[str]:
    """Return Jira projects configured for a repository."""
    repository_config = get_repository_config(
        config,
        repository,
    )

    projects = repository_config.get("jira_projects", [])

    if not isinstance(projects, list):
        raise ValueError(
            f"'jira_projects' for repository '{repository}' must be a list."
        )

    projects = [
        str(project).strip()
        for project in projects
        if str(project).strip()
    ]

    if not projects:
        raise ValueError(
            f"No Jira projects configured for '{repository}'."
        )

    return projects


def get_workflow(
    config: dict[str, Any],
    repository: str,
) -> str:
    """Return the GitHub workflow configured for a repository."""
    repository_config = get_repository_config(
        config,
        repository,
    )

    workflow = repository_config.get("workflow")

    if workflow is None:
        raise ValueError(
            f"No workflow configured for repository '{repository}'."
        )

    workflow = str(workflow).strip()

    if not workflow:
        raise ValueError(
            f"Workflow for repository '{repository}' must not be empty."
        )

    return workflow


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
    operation = (
        f"Jira release creation for '{project_key}'"
    )

    response = jira_request(
        session,
        "POST",
        f"{jira_url}/rest/api/3/version",
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

        return (
            "exists",
            None,
        )

    log_api_error(
        response,
        operation,
    )

    response.raise_for_status()

    raise RuntimeError(
        "Unexpected Jira API response."
    )


def build_release_description(
    environment: dict[str, str],
    workflow: str,
) -> str:
    """Build the Jira release description."""
    return "_".join(
        (
            environment["VERSION"],
            environment["TAG"],
            environment["ENVIRONMENT"],
            environment["REPOSITORY"],
            workflow,
            environment["REF"],
        )
    )


def process_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
    version: str,
    description: str,
) -> str:
    """Create a release for one Jira project."""
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
        project_id = int(
            project["id"]
        )
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


def log_configuration(
    environment: dict[str, str],
    projects: list[str],
    workflow: str,
    description: str,
) -> None:
    """Log release configuration."""
    LOGGER.info(
        "Release configuration | "
        "repository=%s | "
        "version=%s | "
        "tag=%s | "
        "environment=%s | "
        "workflow=%s | "
        "ref=%s | "
        "projects=%s",
        environment["REPOSITORY"],
        environment["VERSION"],
        environment["TAG"],
        environment["ENVIRONMENT"],
        workflow,
        environment["REF"],
        ", ".join(projects),
    )

    LOGGER.info(
        "Release description: %s",
        description,
    )


def main() -> int:
    """Run the Jira release creation process."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        environment = load_environment()

        config = load_configuration(
            CONFIG_FILE
        )

        projects = get_jira_projects(
            config,
            environment["REPOSITORY"],
        )

        workflow = get_workflow(
            config,
            environment["REPOSITORY"],
        )

        description = build_release_description(
            environment,
            workflow,
        )

        log_configuration(
            environment,
            projects,
            workflow,
            description,
        )

        session = create_jira_session(
            environment["JIRA_EMAIL"],
            environment["JIRA_API_TOKEN"],
        )

        created: list[str] = []
        existing: list[str] = []

        for project in projects:
            status = process_project(
                session,
                environment["JIRA_URL"],
                project,
                environment["VERSION"],
                description,
            )

            if status == "created":
                created.append(project)
            else:
                existing.append(project)

        LOGGER.info(
            "Release processing completed | "
            "created=%s | "
            "already_exists=%s",
            ", ".join(created) or "None",
            ", ".join(existing) or "None",
        )

        return 0

    except FileNotFoundError as exc:
        LOGGER.error(
            "%s",
            exc,
        )

    except ValueError as exc:
        LOGGER.error(
            "%s",
            exc,
        )

    except yaml.YAMLError:
        LOGGER.error(
            "The central Jira configuration contains invalid YAML."
        )

    except requests.HTTPError as exc:
        LOGGER.error(
            "Jira API request failed: %s",
            exc,
        )

    except requests.RequestException as exc:
        LOGGER.error(
            "Jira API connection failed: %s",
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
