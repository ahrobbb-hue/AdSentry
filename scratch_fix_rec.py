with open('cogs/recruitment.py', 'rb') as f:
    text = f.read().decode('utf-8')

bad = "self.add_item(RecruitmentPublishChannelSelect(bot, review_channel, grant_role))  self.add_item(RecruitmentRoleSelect(options, public_channel, review_channel, grant_role))"
good = "self.add_item(RecruitmentPublishChannelSelect(bot, review_channel, grant_role))"

if bad in text:
    text = text.replace(bad, good)
    with open('cogs/recruitment.py', 'wb') as f:
        f.write(text.encode('utf-8'))
    print("Cleaned line 251 successfully!")
else:
    print("Bad snippet not found")
