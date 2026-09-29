import os
import sys
from typing import Any

import requests
import yaml


def load_environment() -> dict[str, str]:
    """Load required environment variables."""

    required_names = (
        "VERSION",
        "TAG",
        "ENVIRONMENT",
        "REPOSITORY",
        "REF",
        "JIRA_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "CONFIG_REPO",
        "CONFIG_FILE",
        "CONFIG_REPO_TOKEN",
    )

    environment: dict[str, str] = {}

    for name in required_names:
        value = os.getenv(name)

        if not value:
            raise ValueError(
                f"Required environment variable '{name}' "
                f"is missing or empty."
            )

        environment[name] = value

    return environment


def load_config(
    environment: dict[str, str],
) -> dict[str, Any]:
    """Load jira-projects.yml from the configuration repository."""

    config_url = (
        f"https://raw.githubusercontent.com/"
        f"{environment['CONFIG_REPO']}/main/"
        f"{environment['CONFIG_FILE']}"
    )

    headers = {
        "Authorization": (
            f"Bearer {environment['CONFIG_REPO_TOKEN']}"
        ),
        "Accept": "application/vnd.github+json",
    }

    response = requests.get(
        config_url,
        headers=headers,
        timeout=30,
    )

    response.raise_for_status()

    config = yaml.safe_load(response.text)

    if not isinstance(config, dict):
        raise ValueError(
            "Invalid Jira configuration file."
        )

    return config


def get_repository_config(
    config: dict[str, Any],
    repository: str,
) -> dict[str, Any]:
    """Get configuration for the specified repository."""

    repositories = config.get("repositories")

    if not isinstance(repositories, dict):
        raise ValueError(
            "The 'repositories' section is missing or invalid."
        )

    repository_config = repositories.get(repository)

    if not isinstance(repository_config, dict):
        raise ValueError(
            f"No configuration found for repository "
            f"'{repository}'."
        )

    return repository_config


def get_jira_projects(
    config: dict[str, Any],
    repository: str,
) -> list[str]:
    """Get Jira projects configured for the repository."""

    repository_config = get_repository_config(
        config,
        repository,
    )

    jira_projects = repository_config.get(
        "jira_projects",
        [],
    )

    if not isinstance(jira_projects, list):
        raise ValueError(
            f"'jira_projects' must be a list for "
            f"'{repository}'."
        )

    return [
        str(project).strip()
        for project in jira_projects
        if str(project).strip()
    ]


def get_workflow(
    config: dict[str, Any],
    repository: str,
) -> str:
    """Get deployment workflow from jira-projects.yml."""

    repository_config = get_repository_config(
        config,
        repository,
    )

    workflow = str(
        repository_config.get("workflow", "")
    ).strip()

    if not workflow:
        raise ValueError(
            f"No 'workflow' is configured for repository "
            f"'{repository}' in jira-projects.yml."
        )

    return workflow


def build_release_description(
    environment: dict[str, str],
    workflow: str,
) -> str:
    """
    Build Jira release description.

    Format:

    VERSION_TAG_ENVIRONMENT_REPOSITORY_WORKFLOW_REF
    """

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


def jira_request(
    method: str,
    url: str,
    email: str,
    api_token: str,
    **kwargs: Any,
) -> requests.Response:
    """Make an authenticated Jira REST API request."""

    return requests.request(
        method,
        url,
        auth=(email, api_token),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30,
        **kwargs,
    )


def get_jira_project_id(
    jira_url: str,
    project_key: str,
    email: str,
    api_token: str,
) -> str:
    """Get Jira project ID."""

    url = (
        f"{jira_url}/rest/api/3/project/"
        f"{project_key}"
    )

    response = jira_request(
        "GET",
        url,
        email,
        api_token,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Unable to get Jira project "
            f"'{project_key}'. "
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

    data = response.json()

    return str(data["id"])


def create_jira_version(
    jira_url: str,
    project_id: str,
    version_name: str,
    description: str,
    email: str,
    api_token: str,
) -> bool:
    """Create Jira version."""

    url = f"{jira_url}/rest/api/3/version"

    payload = {
        "project": project_id,
        "name": version_name,
        "description": description,
        "released": False,
    }

    response = jira_request(
        "POST",
        url,
        email,
        api_token,
        json=payload,
    )

    if response.status_code in (200, 201):
        return True

    # Duplicate version should warn and continue.
    if response.status_code == 400:
        try:
            data = response.json()
        except ValueError:
            data = {}

        errors = data.get("errors", {})

        if (
            "name" in errors
            and "already exists"
            in str(errors["name"]).lower()
        ):
            print(
                f"WARNING: Jira version "
                f"'{version_name}' already exists "
                f"in project ID {project_id}. "
                f"Skipping."
            )
            return False

        if "already exists" in response.text.lower():
            print(
                f"WARNING: Jira version "
                f"'{version_name}' already exists "
                f"in project ID {project_id}. "
                f"Skipping."
            )
            return False

    raise RuntimeError(
        f"Failed to create Jira version "
        f"'{version_name}'. "
        f"HTTP {response.status_code}: "
        f"{response.text}"
    )


def main() -> None:
    try:
        environment = load_environment()

        print("========================================")
        print("Jira Release Creation")
        print("========================================")

        print(
            f"Version     : {environment['VERSION']}"
        )
        print(
            f"Tag         : {environment['TAG']}"
        )
        print(
            f"Environment : {environment['ENVIRONMENT']}"
        )
        print(
            f"Repository  : {environment['REPOSITORY']}"
        )
        print(
            f"Git Ref     : {environment['REF']}"
        )

        print("========================================")

        config = load_config(environment)

        repository = environment["REPOSITORY"]

        jira_projects = get_jira_projects(
            config,
            repository,
        )

        workflow = get_workflow(
            config,
            repository,
        )

        description = build_release_description(
            environment,
            workflow,
        )

        version_name = environment["VERSION"]

        print(
            f"Workflow    : {workflow}"
        )

        print(
            f"Description : {description}"
        )

        print("========================================")

        for project_key in jira_projects:

            print(
                f"Processing Jira project: "
                f"{project_key}"
            )

            project_id = get_jira_project_id(
                environment["JIRA_URL"],
                project_key,
                environment["JIRA_EMAIL"],
                environment["JIRA_API_TOKEN"],
            )

            created = create_jira_version(
                environment["JIRA_URL"],
                project_id,
                version_name,
                description,
                environment["JIRA_EMAIL"],
                environment["JIRA_API_TOKEN"],
            )

            if created:
                print(
                    f"SUCCESS: Created Jira version "
                    f"'{version_name}' in "
                    f"{project_key}"
                )
            else:
                print(
                    f"SKIPPED: Jira version "
                    f"'{version_name}' already exists "
                    f"in {project_key}"
                )

        print("========================================")
        print("Release processing completed.")
        print("========================================")

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
