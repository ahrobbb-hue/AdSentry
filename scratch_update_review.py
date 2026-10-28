with open('cogs/recruitment.py', 'r', encoding='utf-8') as f:
    text = f.read()

target = '''            role_result = ""
            if decision == "ACCEPTED" and opening and opening.get("grant_role_id"):
                try:
                    role = interaction.guild.get_role(opening["grant_role_id"])  # type: ignore[union-attr]
                    member = interaction.guild.get_member(application["user_id"])  # type: ignore[union-attr]
                    if not member:
                        member = await interaction.guild.fetch_member(application["user_id"])  # type: ignore[union-attr]
                    if not role or role.is_default() or role.managed or role >= interaction.guild.me.top_role:  # type: ignore[union-attr]
                        raise PermissionError("Configured role is missing or above the bot's role")
                    await member.add_roles(role, reason=f"Accepted for recruitment opening #{opening['id']}")
                    role_result = f" Role {role.name} assigned."
                except (discord.HTTPException, discord.NotFound, PermissionError):
                    logger.exception("Application accepted, but its configured role could not be assigned")
                    role_result = " The application was accepted, but Discord could not assign the configured role."'''

replacement = '''            # 1. Fetch applicant member
            member = interaction.guild.get_member(application["user_id"]) if interaction.guild else None  # type: ignore[union-attr]
            if not member and interaction.guild:
                try:
                    member = await interaction.guild.fetch_member(application["user_id"])  # type: ignore[union-attr]
                except (discord.HTTPException, discord.NotFound):
                    member = None

            # 2. On ACCEPTED or REJECTED: remove temporary applicant role and revoke review channel access
            if decision in {"ACCEPTED", "REJECTED"} and member and interaction.guild:
                applicant_role_name, _ = get_applicant_role_info(opening.get("title", "")) if opening else ("", 1)
                if applicant_role_name:
                    app_role = discord.utils.get(interaction.guild.roles, name=applicant_role_name)
                    if app_role and app_role in member.roles and app_role < interaction.guild.me.top_role and interaction.guild.me.guild_permissions.manage_roles:
                        try:
                            await member.remove_roles(app_role, reason=f"Recruitment #{application_id} {decision}")
                        except discord.HTTPException:
                            logger.exception("Could not remove applicant role %s", applicant_role_name)

                if isinstance(interaction.channel, discord.TextChannel):
                    try:
                        await interaction.channel.set_permissions(member, overwrite=None, reason=f"Recruitment #{application_id} {decision}")
                    except discord.HTTPException:
                        logger.exception("Could not remove channel overwrite for applicant %s", member.id)

            # 3. If ACCEPTED: assign official staff role
            role_result = ""
            if decision == "ACCEPTED" and opening and member and interaction.guild:
                target_role = None
                if opening.get("grant_role_id"):
                    target_role = interaction.guild.get_role(opening["grant_role_id"])
                if not target_role:
                    t_title = opening.get("title", "").strip()
                    target_role = discord.utils.get(interaction.guild.roles, name=t_title)
                    if not target_role:
                        import re
                        clean_t = re.sub(r'^[^\w\s]+', '', t_title).strip()
                        if clean_t:
                            for r in interaction.guild.roles:
                                if r.name.lower() == clean_t.lower() or clean_t.lower() in r.name.lower():
                                    target_role = r
                                    break
                if target_role:
                    try:
                        if not target_role.is_default() and not target_role.managed and target_role < interaction.guild.me.top_role and interaction.guild.me.guild_permissions.manage_roles:
                            await member.add_roles(target_role, reason=f"Accepted for recruitment opening #{opening['id']}")
                            role_result = f" Role **{target_role.name}** assigned."
                        else:
                            role_result = " (Discord could not assign the role: role is higher than bot or bot lacks Manage Roles)."
                    except (discord.HTTPException, discord.NotFound, PermissionError):
                        logger.exception("Application accepted, but its configured role could not be assigned")
                        role_result = " (The application was accepted, but Discord could not assign the role)."'''

if target in text:
    text = text.replace(target, replacement)
    with open('cogs/recruitment.py', 'w', encoding='utf-8') as f:
        f.write(text)
    print("Replaced review block successfully!")
else:
    print("Target block not found!")
