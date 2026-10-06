# Google Drive backup trial for SnapDeploy

This branch is an **experimental** whole-instance backup. It restores `/app/data`
before Lumiverse starts, then uploads encrypted snapshots while the app runs.
The two rotating snapshots are stored in Google Drive's hidden `appDataFolder`.
They include the SQLite database, uploaded files, `owner.credentials`, and
`lumiverse.identity`. Keep the encryption key: a backup cannot be recovered
without it.

## Prerequisites

1. Create a Google Cloud project and enable the Google Drive API.
2. Configure an OAuth consent screen and request only
   `https://www.googleapis.com/auth/drive.appdata`.
3. Create a **Web application** OAuth client with authorized redirect URI
   `https://developers.google.com/oauthplayground`. In the
   [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/),
   open the gear, choose **Use your own OAuth credentials**, enter that client
   ID and secret, and use **Offline** access. Enter exactly the `drive.appdata`
   scope above, authorize your Google account, then exchange the code for a
   refresh token. The client ID, client secret, and refresh token must be put
   in SnapDeploy environment variables, never in GitHub or a committed `.env`
   file. Remove the Playground redirect URI from the OAuth client afterwards.
4. Set the OAuth app to **In production** for sustained use. Google states that
   refresh tokens for external OAuth apps in **Testing** expire after seven
   days. An unverified app may show a Google warning during authorization.
5. Generate a random 32-byte encryption key and encode it as Base64. Store a
   second copy somewhere safe outside SnapDeploy.

## SnapDeploy configuration

Deploy this repository's `drive-backup-trial` branch on port `7860` and set:

| Variable | Value |
| --- | --- |
| `OWNER_PASSWORD` | Your chosen owner password |
| `LUMIVERSE_DRIVE_INSTANCE` | A unique short name, such as `my-lumiverse` |
| `LUMIVERSE_DRIVE_CLIENT_ID` | OAuth client ID |
| `LUMIVERSE_DRIVE_CLIENT_SECRET` | OAuth client secret |
| `LUMIVERSE_DRIVE_REFRESH_TOKEN` | OAuth refresh token |
| `LUMIVERSE_DRIVE_BACKUP_KEY` | Base64-encoded 32-byte key |
| `LUMIVERSE_DRIVE_BACKUP_SECONDS` | Optional; defaults to `300` (five minutes) |

The process refuses to start when any Drive credential is missing or when a
backup exists but cannot be restored. This avoids silently starting with an
empty database. Google Drive must be reachable when the container wakes.

## Trial procedure

1. Start a disposable instance and create a recognizable test chat and image.
2. Wait for `[drive-backup] Uploaded and verified` in SnapDeploy logs.
3. Let the container sleep, then visit it to wake it.
4. Confirm that the same chat, image, owner account, and settings are present.
5. Repeat after a redeploy. Keep your encryption key and OAuth refresh token
   outside the container.

The first backup starts one minute after app launch; later backups occur every
five minutes by default. Changes since the last completed upload can still be
lost. File assets can change while a snapshot is being made, so this trial
does not yet promise fully consistent backups under concurrent uploads.
