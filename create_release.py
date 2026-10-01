"""Create Jira releases from the central GitHub deployment configuration."""

from __future__ import annotations

import base64
import logging
import os
import sys
from typing import Any

import requests
import yaml

LOGGER = logging.getLogger(__name__)
CONFIG_FILE = os.getenv("CONFIG_FILE", "jira-projects.yml")


def load_environment() -> dict[str, str]:
    required = {
        "VERSION": os.getenv("VERSION"),
        "TAG": os.getenv("TAG"),
        "REPOSITORY": os.getenv("REPOSITORY"),
        "REF": os.getenv("REF"),
        "JIRA_URL": os.getenv("JIRA_URL"),
        "JIRA_EMAIL": os.getenv("JIRA_EMAIL"),
        "JIRA_API_TOKEN": os.getenv("JIRA_API_TOKEN"),
    }

    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    return {name: str(value) for name, value in required.items()}


def load_configuration(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    if not isinstance(data, dict):
        raise ValueError("The Jira configuration must contain a YAML object.")

    repositories = data.get("repositories")
    if not isinstance(repositories, dict):
        raise ValueError("The Jira configuration must contain 'repositories'.")

    return data


def get_repository_config(config: dict[str, Any], repository: str) -> dict[str, Any]:
    repositories = config.get("repositories", {})
    repository_config = repositories.get(repository)

    if not isinstance(repository_config, dict):
        raise ValueError(
            f"No configuration found for repository '{repository}'."
        )

    return repository_config


def get_jira_projects(config: dict[str, Any], repository: str) -> list[str]:
    repository_config = get_repository_config(config, repository)
    projects = repository_config.get("jira_projects")

    if not isinstance(projects, list) or not projects:
        raise ValueError(
            f"No Jira projects configured for repository '{repository}'."
        )

    return [str(project).strip() for project in projects if str(project).strip()]


def get_workflows(config: dict[str, Any], repository: str) -> dict[str, str]:
    """Return all configured environment/workflow pairs.

    Environment keys are optional. A repository may have any combination of
    dev, uat, prod, or other environment names.
    """
    repository_config = get_repository_config(config, repository)
    workflows = repository_config.get("workflows")

    if not isinstance(workflows, dict) or not workflows:
        raise ValueError(
            f"No workflows configured for repository '{repository}'."
        )

    result: dict[str, str] = {}
    for environment_name, workflow in workflows.items():
        environment = str(environment_name).strip().lower()
        workflow_name = str(workflow).strip()

        if not environment or not workflow_name:
            continue

        result[environment] = workflow_name

    if not result:
        raise ValueError(
            f"No valid workflows configured for repository '{repository}'."
        )

    return result


def build_release_description(
    environment: dict[str, str],
    workflows: dict[str, str],
) -> str:
    """Build a flexible release description.

    Format:
      VERSION_TAG_REPOSITORY_ENV=WORKFLOW_ENV=WORKFLOW_REF=REF

    Example with all environments:
      1.0.1_v1.0.1_org/repo_dev=testdeploy.yml_uat=uatdeploy.yml_prod=proddeploy.yml_ref=main

    Example with only UAT/PROD:
      1.0.1_v1.0.1_org/repo_uat=uatdeploy.yml_prod=proddeploy.yml_ref=main
    """
    workflow_parts = [
        f"{name}={workflows[name]}"
        for name in sorted(workflows)
    ]

    return "_".join(
        [
            environment["VERSION"],
            environment["TAG"],
            environment["REPOSITORY"],
            *workflow_parts,
            f"ref={environment['REF']}",
        ]
    )


def create_jira_session(email: str, api_token: str) -> requests.Session:
    session = requests.Session()
    session.auth = (email, api_token)
    session.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return session


def process_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
    version: str,
    description: str,
) -> str:
    url = f"{jira_url.rstrip('/')}/rest/api/3/project/{project_key}/versions"

    response = session.get(url, timeout=30)
    response.raise_for_status()
    versions = response.json()

    for existing in versions:
        if existing.get("name") == version:
            LOGGER.warning(
                "Jira version already exists | project: %s | version: %s | continuing",
                project_key,
                version,
            )
            return "existing"

    payload = {
        "description": description,
        "name": version,
        "project": project_key,
        "released": False,
    }

    response = session.post(url, json=payload, timeout=30)
    response.raise_for_status()
    release = response.json()

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
    workflows: dict[str, str],
    description: str,
) -> None:
    LOGGER.info(
        "Release configuration | repository=%s | version=%s | tag=%s | ref=%s | projects=%s",
        environment["REPOSITORY"],
        environment["VERSION"],
        environment["TAG"],
        environment["REF"],
        ", ".join(projects),
    )

    LOGGER.info(
        "Configured workflows: %s",
        ", ".join(f"{name}={workflow}" for name, workflow in sorted(workflows.items())),
    )
    LOGGER.info("Release description: %s", description)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        environment = load_environment()
        config = load_configuration(CONFIG_FILE)
        projects = get_jira_projects(config, environment["REPOSITORY"])
        workflows = get_workflows(config, environment["REPOSITORY"])
        description = build_release_description(environment, workflows)

        log_configuration(environment, projects, workflows, description)

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
            "Release processing completed | created=%s | already_exists=%s",
            ", ".join(created) or "None",
            ", ".join(existing) or "None",
        )
        return 0

    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
    except ValueError as exc:
        LOGGER.error("%s", exc)
    except yaml.YAMLError:
        LOGGER.error("The central Jira configuration contains invalid YAML.")
    except requests.HTTPError as exc:
        LOGGER.error("Jira API request failed: %s", exc)
    except requests.RequestException as exc:
        LOGGER.error("Jira API connection failed: %s", exc)
    except KeyboardInterrupt:
        LOGGER.error("Process interrupted by user.")
        return 130
    except Exception:
        LOGGER.exception("Unexpected error while creating Jira releases.")

    return 1


if __name__ == "__main__":
    sys.exit(main())
