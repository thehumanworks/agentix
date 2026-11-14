from . import ssh
from .image import IMAGE_NAME, build_image
from .sandbox import create_sandbox, execute, list_sandboxes, terminate


def _build_image(force_build: bool = False, image_name: str = IMAGE_NAME):
    image = build_image(force_build=force_build, image_name=image_name)
    return {"image_id": image.object_id}

def _create_sandbox(
    image_name: str = IMAGE_NAME,
    secret_name: str = "1password",
    timeout: int = 3600,
    workdir: str = "",
):
    sb = create_sandbox(
        image_name=image_name,
        secret_name=secret_name,
        timeout=timeout,
        workdir=workdir,
    )
    return {"sandbox_id": sb.object_id}


def _terminate_sandbox(sandbox_id: str):
    return terminate(sandbox_id=sandbox_id)


def _execute(sandbox_id: str, command: str):
    return execute(command=command, sandbox_id=sandbox_id)


def _list_sandboxes():
    return list_sandboxes()


def main():
    from fire import Fire

    Fire(
        {
            "image": {
                "build": _build_image,
            },
            "sandbox": {
                "up": _create_sandbox,
                "down": _terminate_sandbox,
                "exec": _execute,
                "ls": _list_sandboxes,
                "ssh": {
                    "start": ssh.start,
                    "stop": ssh.stop,
                    "status": ssh.status,
                },
            },
        }
    )


if __name__ == "__main__":
    import os

    os.environ["PAGER"] = "cat"
    main()
