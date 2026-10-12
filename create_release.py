"""Create Jira releases using GitHub repository custom properties."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any
from urllib.parse import quote, urlparse

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
SUPPORTED_DEPLOYMENT_TYPES = {DEPLOYMENT_TYPE_DIRECT, DEPLOYMENT_TYPE_TEMPLATE}
WORKFLOW_PROPERTY_NAMES = ("workflow", "workflows")

VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
WORKFLOW_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$", re.IGNORECASE)

LOG = logging.getLogger("create_release")


def required_env(name: str) -> str:
    """Return a required environment variable or raise an actionable error."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(
            f"Required environment variable '{name}' is missing or empty. "
            "Check the workflow inputs and repository secrets."
        )
    return value


def validate_environment(environment: dict[str, str]) -> None:
    """Validate release inputs before making API calls."""
    version = environment["VERSION"]
    tag = environment["TAG"]
    repository = environment["REPOSITORY"]
    ref = environment["REF"]
    jira_url = environment["JIRA_URL"]

    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            f"Invalid Jira release version '{version}'. Use letters, numbers, dots, "
            "underscores, plus signs, or hyphens; the first character must be a letter or number."
        )

    if (
        not tag
        or tag.startswith("-")
        or tag.startswith("/")
        or tag.endswith("/")
        or tag.endswith(".")
        or ".." in tag
        or "@{" in tag
        or any(character.isspace() for character in tag)
        or any(character in tag for character in ("~", "^", ":", "?", "*", "[", "\\"))
        or any(part.startswith(".") or part.endswith(".lock") for part in tag.split("/"))
    ):
        raise ValueError(
            f"Invalid Git tag '{tag}'. Use a valid Git tag name without spaces or forbidden characters."
        )

    if (
        not ref
        or ref.startswith("-")
        or ref.startswith("/")
        or ref.endswith("/")
        or ref.endswith(".")
        or ".." in ref
        or "@{" in ref
        or any(character.isspace() for character in ref)
        or any(character in ref for character in ("~", "^", ":", "?", "*", "[", "\\"))
        or any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
    ):
        raise ValueError(
            f"Invalid source ref '{ref}'. Provide a valid branch name or tag name."
        )

    repository_parts = repository.split("/")
    if (
        len(repository_parts) != 2
        or not all(repository_parts)
        or any(part in {".", ".."} for part in repository_parts)
        or any(character.isspace() for character in repository)
    ):
        raise ValueError(
            f"Invalid repository '{repository}'. Expected the format 'owner/repository'."
        )

    parsed_url = urlparse(jira_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError(
            f"Invalid JIRA_URL '{jira_url}'. Provide a complete HTTPS Jira site URL."
        )


def load_environment() -> dict[str, str]:
    """Load and validate required environment values."""
    environment = {
        "VERSION": required_env("VERSION"),
        "TAG": required_env("TAG"),
        "REPOSITORY": required_env("REPOSITORY"),
        "REF": required_env("REF"),
        "JIRA_URL": required_env("JIRA_URL").rstrip("/"),
        "JIRA_EMAIL": required_env("JIRA_EMAIL"),
        "JIRA_API_TOKEN": required_env("JIRA_API_TOKEN"),
        "GITHUB_TOKEN": required_env("GITHUB_TOKEN"),
    }
    validate_environment(environment)
    return environment


def parse_repository(repository: str) -> tuple[str, str]:
    """Split an owner/repository value into its two components."""
    parts = repository.strip().split("/", maxsplit=1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f"Invalid repository '{repository}'. Expected the format 'owner/repository'."
        )
    return parts[0], parts[1]


def create_github_session(token: str) -> requests.Session:
    """Create an authenticated GitHub API session."""
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "jira-release-automation",
        }
    )
    return session


def github_request(
    session: requests.Session,
    method: str,
    endpoint: str,
    **kwargs: Any,
) -> Response:
    """Send a GitHub API request and return its response."""
    url = endpoint if endpoint.startswith("https://") else f"{GITHUB_API_BASE}{endpoint}"
    try:
        response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"GitHub API request failed for {url}: {exc}") from exc
    if not response.ok:
        log_github_api_error(response, method, url)
        raise RuntimeError(
            f"GitHub API request failed with HTTP {response.status_code} for {method} {url}. "
            f"{json_value(response, 'message') or response.text[:500] or 'No response body.'}"
        )
    return response


def log_github_api_error(response: Response, method: str, url: str) -> None:
    """Log a useful GitHub API error without exposing credentials."""
    message = json_value(response, "message")
    if response.status_code == 401:
        guidance = "The GitHub token is invalid, expired, or not accepted."
    elif response.status_code == 403:
        guidance = (
            "The token may lack permission to read repository custom properties, "
            "or organization policy/SSO approval may block access."
        )
    elif response.status_code == 404:
        guidance = (
            "The repository may not exist at this owner/name, or the token may not have access to it."
        )
    else:
        guidance = "Check the GitHub API response and repository access configuration."
    LOG.error(
        "GitHub API error: %s %s returned HTTP %s. %s Response: %s",
        method,
        url,
        response.status_code,
        guidance,
        message or response.text[:500] or "No response body.",
    )


def json_value(response: Response, key: str) -> Any:
    """Safely return a value from a JSON response object."""
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError):
        return None
    return payload.get(key) if isinstance(payload, dict) else None


def get_repository_properties(
    session: requests.Session, repository: str
) -> dict[str, Any]:
    """Read repository custom properties from the GitHub REST API."""
    owner, repo = parse_repository(repository)
    endpoint = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/properties/values"
    try:
        response = github_request(session, "GET", endpoint)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Unable to read custom properties for repository '{repository}'. {exc}"
        ) from exc

    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError) as exc:
        raise RuntimeError(
            f"GitHub returned a non-JSON response while reading custom properties for '{repository}'."
        ) from exc

    if not isinstance(payload, list):
        raise RuntimeError(
            f"Unexpected custom-properties response for '{repository}': expected a JSON list."
        )

    properties: dict[str, Any] = {}
    for item in payload:
        if not isinstance(item, dict):
            LOG.warning("Ignoring an invalid custom property entry for '%s'.", repository)
            continue
        name = item.get("property_name")
        if not isinstance(name, str) or not name.strip():
            LOG.warning("Ignoring a custom property entry without property_name for '%s'.", repository)
            continue
        properties[name.strip().lower()] = item.get("value")

    if not properties:
        raise ValueError(
            f"Repository '{repository}' returned no readable custom properties. "
            "Confirm that the properties are configured and the GitHub token can read them."
        )
    return properties


def get_property_value(properties: dict[str, Any], property_name: str) -> Any:
    """Return a property value using case-insensitive property-name matching."""
    return properties.get(property_name.strip().lower())


def get_deployment_type(properties: dict[str, Any]) -> str:
    """Read and validate the deployment strategy property."""
    raw_value = get_property_value(properties, DEPLOYMENT_TYPE_PROPERTY)
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        LOG.info("Custom property '%s' is not set; defaulting to 'direct'.", DEPLOYMENT_TYPE_PROPERTY)
        return DEPLOYMENT_TYPE_DIRECT

    if not isinstance(raw_value, str):
        raise ValueError(
            f"Custom property '{DEPLOYMENT_TYPE_PROPERTY}' must be a string containing 'direct' or 'template'."
        )
    deployment_type = raw_value.strip().lower()
    if deployment_type not in SUPPORTED_DEPLOYMENT_TYPES:
        supported = ", ".join(sorted(SUPPORTED_DEPLOYMENT_TYPES))
        raise ValueError(
            f"Unsupported deployment type '{raw_value}'. Custom property "
            f"'{DEPLOYMENT_TYPE_PROPERTY}' must be one of: {supported}."
        )
    return deployment_type


def split_configuration_values(value: Any, property_name: str) -> list[str]:
    """Convert a supported string/list property format into trimmed values."""
    if isinstance(value, list):
        values = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        values = [item.strip() for item in re.split(r"[,;|\n\r]+", value) if item.strip()]
    else:
        raise ValueError(
            f"Custom property '{property_name}' must be a string or list, but received {type(value).__name__}."
        )
    return values


def get_jira_projects(properties: dict[str, Any]) -> list[str]:
    """Read and validate Jira project keys from repository custom properties."""
    raw_value = get_property_value(properties, JIRA_PROJECTS_PROPERTY)
    if raw_value is None or raw_value == "":
        raise ValueError(
            f"Required repository custom property '{JIRA_PROJECTS_PROPERTY}' is missing or empty. "
            "Set it to Jira project keys such as 'APPDEPLOY,SCRUM'."
        )

    values = split_configuration_values(raw_value, JIRA_PROJECTS_PROPERTY)
    if not values:
        raise ValueError(
            f"Repository custom property '{JIRA_PROJECTS_PROPERTY}' does not contain any Jira project keys."
        )

    projects: list[str] = []
    seen: set[str] = set()
    for value in values:
        project = value.upper()
        if not PROJECT_KEY_PATTERN.fullmatch(project):
            raise ValueError(
                f"Invalid Jira project key '{value}' in custom property '{JIRA_PROJECTS_PROPERTY}'. "
                "Project keys must start with a letter and contain only letters, numbers, or underscores."
            )
        if project not in seen:
            projects.append(project)
            seen.add(project)
    return projects


def validate_workflow_filename(filename: str) -> str:
    """Validate a workflow filename and return its normalized value."""
    filename = filename.strip()
    if not WORKFLOW_FILE_PATTERN.fullmatch(filename) or "/" in filename or "\\" in filename:
        raise ValueError(
            f"Invalid workflow filename '{filename}'. Use a workflow filename ending in .yml or .yaml, "
            "for example 'deploy.yml'. Do not include a directory path."
        )
    return filename


def get_workflows(properties: dict[str, Any]) -> dict[str, str]:
    """Read workflow configuration as either one filename or environment mappings."""
    raw_value = None
    selected_property = None
    for property_name in WORKFLOW_PROPERTY_NAMES:
        candidate = get_property_value(properties, property_name)
        if candidate is not None and candidate != "":
            raw_value = candidate
            selected_property = property_name
            break

    if raw_value is None:
        accepted = " or ".join(f"'{name}'" for name in WORKFLOW_PROPERTY_NAMES)
        raise ValueError(
            f"Required repository custom property {accepted} is missing or empty. "
            "Set it to a workflow filename or environment mappings such as "
            "'uat=uatdeploy.yml|prod=proddeploy.yml'."
        )

    if isinstance(raw_value, list):
        raw_entries = [str(item).strip() for item in raw_value if str(item).strip()]
    elif isinstance(raw_value, str):
        raw_text = raw_value.strip()
        # A lone workflow filename is the supported direct-deployment shorthand.
        if WORKFLOW_FILE_PATTERN.fullmatch(raw_text):
            return {"default": validate_workflow_filename(raw_text)}
        raw_entries = [item.strip() for item in re.split(r"[,|\n\r]+", raw_text) if item.strip()]
    else:
        raise ValueError(
            f"Custom property '{selected_property}' must be a workflow filename, mapping string, or list."
        )

    workflows: dict[str, str] = {}
    for entry in raw_entries:
        if "=" in entry:
            environment, filename = entry.split("=", maxsplit=1)
        elif ":" in entry:
            environment, filename = entry.split(":", maxsplit=1)
        else:
            raise ValueError(
                f"Invalid workflow mapping '{entry}' in custom property '{selected_property}'. "
                "Use 'environment=workflow.yml', for example 'uat=uatdeploy.yml'."
            )

        environment = environment.strip().lower()
        filename = filename.strip()
        if not environment or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", environment):
            raise ValueError(
                f"Invalid environment name '{environment}' in custom property '{selected_property}'."
            )
        if environment in workflows:
            raise ValueError(
                f"Duplicate workflow environment '{environment}' in custom property '{selected_property}'."
            )
        workflows[environment] = validate_workflow_filename(filename)

    if not workflows:
        raise ValueError(
            f"Custom property '{selected_property}' does not contain any valid workflow mappings."
        )
    return workflows


def create_jira_session(email: str, api_token: str) -> requests.Session:
    """Create an authenticated Jira API session."""
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, api_token)
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "jira-release-automation",
        }
    )
    return session


def jira_request(
    session: requests.Session,
    jira_url: str,
    method: str,
    endpoint: str,
    **kwargs: Any,
) -> Response:
    """Send a Jira REST API request and return its response."""
    url = f"{jira_url.rstrip('/')}{endpoint}"
    try:
        response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise RuntimeError(f"Jira API request failed for {url}: {exc}") from exc
    if not response.ok:
        log_api_error(response, method, url)
        message = json_value(response, "errorMessages")
        if isinstance(message, list):
            message = "; ".join(str(item) for item in message)
        if not message:
            errors = json_value(response, "errors")
            message = errors or json_value(response, "message")
        raise RuntimeError(
            f"Jira API request failed with HTTP {response.status_code} for {method} {url}. "
            f"{message or response.text[:500] or 'No response body.'}"
        )
    return response


def log_api_error(response: Response, method: str, url: str) -> None:
    """Log Jira API errors with permission and input guidance."""
    if response.status_code == 401:
        guidance = "Check JIRA_EMAIL and JIRA_API_TOKEN."
    elif response.status_code == 403:
        guidance = "The Jira account may not have permission to browse the project or administer versions."
    elif response.status_code == 404:
        guidance = "Confirm the Jira site URL, project key, and REST API endpoint."
    elif response.status_code == 400:
        guidance = "Check the Jira project key, release version, and request fields."
    else:
        guidance = "Check the Jira API response for details."
    LOG.error(
        "Jira API error: %s %s returned HTTP %s. %s Response: %s",
        method,
        url,
        response.status_code,
        guidance,
        response.text[:500] or "No response body.",
    )


def json_object(response: Response) -> dict[str, Any]:
    """Decode a JSON object response, rejecting unexpected payloads."""
    try:
        payload = response.json()
    except (ValueError, requests.JSONDecodeError) as exc:
        raise RuntimeError("The API returned a response that is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("The API returned an unexpected JSON response; expected an object.")
    return payload


def get_jira_project(
    session: requests.Session, jira_url: str, project_key: str
) -> dict[str, Any]:
    """Confirm that the configured Jira project exists and is accessible."""
    endpoint = f"/rest/api/3/project/{quote(project_key, safe='')}"
    try:
        response = jira_request(session, jira_url, "GET", endpoint)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Unable to access Jira project '{project_key}'. Verify that it exists and the Jira account has access. {exc}"
        ) from exc
    return json_object(response)


def is_duplicate_version(
    session: requests.Session, jira_url: str, project_key: str, version: str
) -> bool:
    """Check whether the Jira project already contains the release version."""
    endpoint = f"/rest/api/3/project/{quote(project_key, safe='')}/versions"
    response = jira_request(session, jira_url, "GET", endpoint, params={"maxResults": 1000})
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(
            f"Unexpected version-list response for Jira project '{project_key}': expected a JSON list."
        )
    return any(
        isinstance(item, dict) and str(item.get("name", "")).strip() == version
        for item in payload
    )


def create_jira_version(
    session: requests.Session,
    jira_url: str,
    project_key: str,
    version: str,
    description: str,
) -> dict[str, Any]:
    """Create a Jira project version with the release metadata description."""
    endpoint = "/rest/api/3/version"
    payload = {
        "name": version,
        "project": project_key,
        "description": description,
    }
    response = jira_request(session, jira_url, "POST", endpoint, json=payload)
    return json_object(response)


def build_release_description(
    version: str,
    tag: str,
    repository: str,
    deployment_type: str,
    workflows: dict[str, str],
    ref: str,
) -> str:
    """Build the pipe-delimited description consumed by the Forge deployment app."""
    workflow_value = ",".join(
        f"{environment}:{filename}"
        for environment, filename in workflows.items()
    )
    return (
        f"version={version}|tag={tag}|repository={repository}|"
        f"deploymentType={deployment_type}|workflows={workflow_value}|ref={ref}"
    )


def process_project(
    session: requests.Session,
    jira_url: str,
    project_key: str,
    version: str,
    description: str,
) -> bool:
    """Create a release in one Jira project, skipping an existing version."""
    LOG.info("Processing Jira project '%s'.", project_key)
    get_jira_project(session, jira_url, project_key)

    if is_duplicate_version(session, jira_url, project_key, version):
        LOG.warning(
            "Jira version '%s' already exists in project '%s'; skipping creation and continuing.",
            version,
            project_key,
        )
        return True

    created = create_jira_version(session, jira_url, project_key, version, description)
    LOG.info(
        "Created Jira version '%s' in project '%s' (version ID: %s).",
        version,
        project_key,
        created.get("id", "not returned"),
    )
    return True


def log_configuration(
    environment: dict[str, str],
    projects: list[str],
    deployment_type: str,
    workflows: dict[str, str],
) -> None:
    """Log non-secret release configuration for build diagnostics."""
    LOG.info("Repository          : %s", environment["REPOSITORY"])
    LOG.info("Jira version        : %s", environment["VERSION"])
    LOG.info("Release tag         : %s", environment["TAG"])
    LOG.info("Release source ref  : %s", environment["REF"])
    LOG.info("Deployment type     : %s", deployment_type)
    LOG.info("Jira projects       : %s", ", ".join(projects))
    LOG.info(
        "Workflow mappings   : %s",
        ", ".join(f"{key}={value}" for key, value in workflows.items()),
    )
    LOG.info("Jira site           : %s", environment["JIRA_URL"])


def main() -> int:
    """Load configuration and create the Jira release in each configured project."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s: %(message)s",
    )

    try:
        environment = load_environment()
        github_session = create_github_session(environment["GITHUB_TOKEN"])
        properties = get_repository_properties(github_session, environment["REPOSITORY"])
        projects = get_jira_projects(properties)
        deployment_type = get_deployment_type(properties)
        workflows = get_workflows(properties)
        log_configuration(environment, projects, deployment_type, workflows)

        description = build_release_description(
            version=environment["VERSION"],
            tag=environment["TAG"],
            repository=environment["REPOSITORY"],
            deployment_type=deployment_type,
            workflows=workflows,
            ref=environment["REF"],
        )

        jira_session = create_jira_session(
            environment["JIRA_EMAIL"], environment["JIRA_API_TOKEN"]
        )
        failed_projects: list[str] = []
        for project_key in projects:
            try:
                process_project(
                    session=jira_session,
                    jira_url=environment["JIRA_URL"],
                    project_key=project_key,
                    version=environment["VERSION"],
                    description=description,
                )
            except (RuntimeError, ValueError, requests.RequestException) as exc:
                failed_projects.append(project_key)
                LOG.error("Failed to process Jira project '%s': %s", project_key, exc)

        if failed_projects:
            LOG.error("Release creation failed for project(s): %s", ", ".join(failed_projects))
            return 1

        LOG.info("Jira release processing completed for all configured projects.")
        return 0

    except (ValueError, RuntimeError, requests.RequestException) as exc:
        LOG.error("Release configuration or API operation failed: %s", exc)
        return 1
    except Exception:
        LOG.exception("Unexpected error while creating Jira releases.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
