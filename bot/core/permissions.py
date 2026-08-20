def is_officer(member):
    if not hasattr(member, "roles"):
        return False
    valid_roles = ["officer", "guild master", "admin", "phó hội", "chủ hội", "bot"]
    return any(r.name.lower() in valid_roles for r in member.roles)
