# ORVIX GROUP HELP BOT — Firebase RTDB build

## Files to upload to GitHub
- `main.py`
- `requirements.txt`
- `render.yaml`

Do NOT upload Firebase service-account JSON or bot token to GitHub.

## Render Environment Variables

Set these in Render → your Web Service → Environment:

1. `BOT_TOKEN` — Telegram bot token from @BotFather.
2. `OWNER_ID` — your numeric Telegram user ID.
3. `FIREBASE_DATABASE_URL` — exact Realtime Database URL shown in Firebase Console.
4. `FIREBASE_SERVICE_ACCOUNT` — entire service-account JSON on one line.
5. `WEBHOOK_URL` — Render public service URL, e.g. `https://orvix-group-help-bot.onrender.com` (no trailing slash).
6. `WEBHOOK_SECRET` — random string using only A-Z, a-z, 0-9, underscore or hyphen.
7. `PORT` — optional; Render supplies this automatically.

## Firebase setup
1. Firebase Console → create/select project.
2. Build → Realtime Database → Create Database.
3. Copy the database URL.
4. Realtime Database → Rules; paste:
   {
     "rules": {
       ".read": false,
       ".write": false
     }
   }
5. Project Settings → Service Accounts → Generate new private key.
6. Keep that downloaded JSON private. Paste its entire contents into Render's `FIREBASE_SERVICE_ACCOUNT` value. Do not upload it to GitHub or share it in chat.
7. Enable the Firebase Realtime Database API if prompted.

Firebase Admin SDK on the server authenticates using the service account and accesses RTDB with administrative privileges; the restrictive client rules protect direct client access.

## Telegram setup
1. Create bot with @BotFather.
2. Add it to your groups and promote it to admin.
3. Enable Delete Messages, Restrict Members, Ban Users and Pin Messages.
4. For normal link detection, use @BotFather `/setprivacy` → Disable, then remove and re-add the bot to each group.
5. Group admins manage their own group's link protection and punishment thresholds from 🛡 Group Settings.
6. Bot owner uses 👑 Owner Panel in private chat for All Groups or Selected Group broadcasts.

## Deployment
1. Upload these three files to a GitHub repository.
2. Render → New → Web Service → connect that repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python main.py`
5. Add all environment variables above and deploy.

## Notes
- Firebase RTDB stores group metadata, settings, warning counts, link violation counts, moderation logs and broadcast history.
- Media itself is not uploaded to RTDB. Telegram `file_id` is used when sending broadcast media.
- Render Free services can sleep after inactivity; webhook traffic wakes them but continuous availability is not guaranteed.
- Test in a private test group first. Bot permissions and group reaction settings affect moderation/pinning.
- `Unlimited` means no automatic punishment based on the link counter; link deletion and warning recording remain active.
- Warning thresholds are checked at their exact configured count. Keep thresholds distinct and increasing (e.g. mute 3, ban 5, kick 7).
- This is a starter implementation and should be tested before using it in a large public group.


## Updated features (v2)
- Group Settings and Moderation can be opened in private chat; the bot lists active groups where the user is a Telegram admin.
- Moderation supports Warn, Mute, Unmute, Ban, Unban, and Kick by numeric Telegram user ID.
- Group creator receives a private moderation alert for warnings and punishments, with quick Unmute/Unban buttons when applicable.
- The group creator must first open the bot privately and send `/start`; Telegram does not let bots initiate private conversations.
- Keep the bot as group admin with Delete Messages, Restrict Members, Ban Users, and Pin Messages permissions as needed.
