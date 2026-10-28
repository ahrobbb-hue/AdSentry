with open('cogs/recruitment.py', 'r', encoding='utf-8') as f:
    text = f.read()

import re
# Look for the f"📢 **Selected Announcement Channel:** ... string
pattern = r'await interaction\.response\.send_message\(\s*f"📢[^\)]+\)'
replacement = 'await interaction.response.send_message(f"📢 **Selected Announcement Channel:** {public_channel.mention}\\n**Step 2:** Choose the recruitment role from the list below:", view=RecruitmentRoleSelectView(options, public_channel, review_channel, grant_role), ephemeral=True)'

text = re.sub(pattern, replacement, text)

with open('cogs/recruitment.py', 'w', encoding='utf-8') as f:
    f.write(text)

print("Cleaned line 662!")
