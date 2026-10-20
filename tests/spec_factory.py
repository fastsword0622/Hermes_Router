from hermes_router.specification import WorkSpec


def minimal_spec(**changes):
    body = dict(objective="Implement a tested function", functional_requirements=[{"id": "F1", "description": "return expected value"}],
        non_functional_requirements=[{"id": "N1", "description": "Python compatible"}],
        allowed_files=["app.py"], forbidden_changes=["do not change dependencies"],
        acceptance_criteria=[{"id": "A1", "description": "function and compatibility checked",
            "requirement_ids": ["F1", "N1"], "verification_command_ids": ["C1"]}],
        verification_commands=[{"id": "C1", "argv": ["python3", "-c", "assert 1+1==2"]}], risk_level="low")
    body.update(changes)
    return WorkSpec.model_validate(body)
