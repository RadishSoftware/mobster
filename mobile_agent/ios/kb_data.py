"""The iOS knowledge base's entries (edited by hand; see kb.py for what they are and where they come from).

Distilled in our own words on 14 Oct 2026 from Apple's iPhone User Guide for iOS 26
(support.apple.com/guide/iphone/<page>/26/ios/26) and iOS 18 (/18.0/ios/18.0), and from screens recorded on an iOS 26.4
simulator and an iPhone 15 Pro on iOS 26.0.1."""

ENTRIES = [{'id': 'alert.allow_paste',
  'topic': 'alert',
  'title': 'Allow Paste prompt',
  'ios': [26, 26],
  'text': 'Title: "“App” would like to paste from “Source”". Buttons: "Allow Paste" and "Don\'t Allow Paste". It '
          'appears when an app reads the clipboard. Allow only your own copied text. Permanent choice: Settings › '
          'Apps › app › "Paste from Other Apps" (iOS 18+; earlier under the app\'s own settings).',
  'keywords': ['paste permission', 'clipboard access', 'paste from other apps'],
  'screens': ['alert'],
  'source': ['select-and-edit-text-iph1a9cae52c']},
 {'id': 'alert.allow_paste.ios18',
  'topic': 'alert',
  'title': 'Allow Paste prompt (iOS 18)',
  'ios': [18, 18],
  'text': 'Title: "“App” would like to paste from “Source”". Buttons: "Allow Paste" and "Don\'t Allow Paste". Allow '
          'only text you copied yourself. Permanent choice: Settings › Apps › app › "Paste from Other Apps".',
  'keywords': ['paste permission', 'clipboard access', 'paste from other apps'],
  'screens': ['alert'],
  'source': ['select-and-edit-text-iph1a9cae52c']},
 {'id': 'alert.app_store.install',
  'topic': 'alert',
  'title': 'App Store install confirm',
  'ios': [18, 26],
  'text': '"Get" or the cloud button starts a download; paid apps and some installs show a confirm sheet with '
          '"Install" and "Double-click to Pay" or "Confirm with Side Button". Double-click the side button, then '
          'Face ID. Buying and installing are consequential; ask first.',
  'keywords': ['get app', 'buy app', 'install app', 'side button'],
  'screens': ['alert'],
  'source': ['sign-in-and-manage-your-apple-account-iph76e54c61e']},
 {'id': 'alert.app_store.rating',
  'topic': 'alert',
  'title': 'Rating and Open in App Store',
  'ios': [18, 26],
  'text': '"Enjoying “App”?" with stars or buttons; "Not Now" or "Cancel" closes it. A link may ask "Open in App '
          'Store?" with "Cancel" and "Open". Close rating prompts; opening the store from a link is harmless but '
          'changes app.',
  'keywords': ['rate app', 'review prompt', 'app store link'],
  'screens': ['alert'],
  'source': ['sign-in-and-manage-your-apple-account-iph76e54c61e']},
 {'id': 'alert.apple_account',
  'topic': 'alert',
  'title': 'Apple Account sign-in prompts',
  'ios': [18, 26],
  'text': 'Sheets titled "Sign in to iTunes Store", "Apple Account Password" or "Sign In with Apple Account" ask for '
          "the user's password or a six-digit verification code. Never type credentials for them; leave to the user. "
          '"Sign in with Apple" sheets pick an email-hiding option and confirm with Face ID or passcode.',
  'keywords': ['apple id password', 'icloud sign in', 'verification code', 'two-factor'],
  'screens': ['alert'],
  'source': ['sign-in-and-manage-your-apple-account-iph76e54c61e']},
 {'id': 'alert.ios_update',
  'topic': 'alert',
  'title': 'iOS update nag',
  'ios': [18, 26],
  'text': 'An alert says an iOS update is available, with "Install Now", "Later" or "Remind Me Later", and "Install '
          'Tonight". Choose "Later" or "Remind Me Later"; installing restarts the phone. Update manually in Settings '
          '› General › Software Update ("Download and Install").',
  'keywords': ['software update', 'update available', 'install tonight', 'automatic updates'],
  'screens': ['alert'],
  'source': ['update-ios-iph3e504502']},
 {'id': 'alert.low_battery',
  'topic': 'alert',
  'title': 'Low Battery alert',
  'ios': [18, 26],
  'text': 'Title: "Low Battery" (20% and 10% left), buttons "Close" and "Low Power Mode". Close it; the alert '
          'changes nothing. Low Power Mode lowers brightness and background work.',
  'keywords': ['battery low', 'low power mode', '20 percent'],
  'screens': ['alert'],
  'source': ['charge-the-battery-iph63eecc618']},
 {'id': 'alert.open_in_app',
  'topic': 'alert',
  'title': 'Open in app and Sign In prompts',
  'ios': [18, 26],
  'text': 'After a link: "Open in “App”?" with "Cancel" and "Open". Web sign-in: "“App” Wants to Use “domain” to '
          'Sign In" with "Cancel" and "Continue"; continuing lets the app see sign-in info for that site. Open only '
          'links the task started.',
  'keywords': ['universal link', 'open link', 'use domain to sign in', 'web authentication'],
  'screens': ['alert'],
  'source': ['control-access-to-information-in-apps-iph251e92810']},
 {'id': 'alert.permission.contacts',
  'topic': 'alert',
  'title': 'Contacts permission',
  'ios': [18, 26],
  'text': 'Title: "“App” Would Like to Access Your Contacts". Buttons: "Allow Full Access", "Don\'t Allow"; iOS 18 '
          "adds a choice to select contacts (limited access). Granting is the user's call. Edit later: Settings › "
          'Privacy & Security › Contacts › app › "Edit Selected Contacts".',
  'keywords': ['limited contacts', 'address book', 'select contacts'],
  'screens': ['alert'],
  'source': ['control-access-to-contacts-iph9536aa9a5']},
 {'id': 'alert.permission.hardware',
  'topic': 'alert',
  'title': 'Camera, microphone, Bluetooth, local network',
  'ios': [18, 26],
  'text': 'Titles read "“App” Would Like to Access the Camera" or the Microphone, "Use Bluetooth", "Find and Connect '
          'to Devices on Your Local Network". Buttons: "Allow" and "Don\'t Allow" (OK in some). Hardware grants are '
          "the user's call. Review them in Settings › Privacy & Security.",
  'keywords': ['camera access', 'mic access', 'bluetooth permission', 'local network permission'],
  'screens': ['alert'],
  'source': ['control-access-to-hardware-features-iph168c4bbd5']},
 {'id': 'alert.permission.location',
  'topic': 'alert',
  'title': 'Location permission',
  'ios': [18, 26],
  'text': 'Title: "Allow “App” to use your location?". Buttons: "Allow Once", "Allow While Using App", "Don\'t '
          'Allow"; a "Precise: On" toggle sends exact location, off sends approximate. Granting private data is the '
          "user's call. Later change: Settings › Privacy & Security › Location Services.",
  'keywords': ['gps', 'precise location', 'where am I', 'location services'],
  'screens': ['alert'],
  'source': ['control-the-location-information-you-share-iph3dd5f9be']},
 {'id': 'alert.permission.notifications',
  'topic': 'alert',
  'title': 'Notifications permission',
  'ios': [18, 26],
  'text': 'Title: "“App” Would Like to Send You Notifications". Buttons: "Allow", "Don\'t Allow". Declining is safe '
          'and the app still works. Change it in Settings › Notifications › app.',
  'keywords': ['push notifications', 'notify me', 'allow notifications'],
  'screens': ['alert'],
  'source': ['control-access-to-information-in-apps-iph251e92810']},
 {'id': 'alert.permission.photos',
  'topic': 'alert',
  'title': 'Photos permission',
  'ios': [18, 26],
  'text': 'Title: "“App” Would Like to Access Your Photos". iOS 18+ buttons: "Limit Access...", "Allow Full Access", '
          '"Don\'t Allow"; limited access lets you pick photos. Granting is the user\'s call. Change it in Settings '
          '› Privacy & Security › Photos.',
  'keywords': ['photo library', 'limited access', 'select photos', 'camera roll'],
  'screens': ['alert'],
  'source': ['control-access-to-information-in-apps-iph251e92810']},
 {'id': 'alert.permission.tracking',
  'topic': 'alert',
  'title': 'Tracking permission',
  'ios': [18, 26],
  'text': 'Title: "Allow “App” to track your activity across other companies’ apps and websites?". Buttons: "Ask App '
          'Not to Track" and "Allow". Declining is safe. Settings › Privacy & Security › Tracking lists apps and the '
          '"Allow Apps to Request to Track" switch.',
  'keywords': ['app tracking transparency', 'ads', 'track me'],
  'screens': ['alert'],
  'source': ['control-app-tracking-permissions-iph4f4cbd242']},
 {'id': 'alert.save_password',
  'topic': 'alert',
  'title': 'Save Password and AutoFill',
  'ios': [18, 26],
  'text': 'After a sign-in a sheet offers "Save Password" (or "Save in Passwords") and "Not Now" / "Never". Saving '
          "is the user's call; decline by default. A password bar above the keyboard offers saved logins; tapping it "
          'fills with Face ID. Passwords app: all saved credentials.',
  'keywords': ['passwords app', 'autofill', 'keychain', 'strong password'],
  'screens': ['alert', 'keyboard'],
  'source': ['use-passwords-iphd18e98624']},
 {'id': 'alert.screen_time_limit',
  'topic': 'alert',
  'title': 'Screen Time limit screen',
  'ios': [18, 26],
  'text': 'A full-screen "Time Limit" page says you reached the limit for an app, with buttons such as "Ignore '
          'Limit", "Remind Me in 15 Minutes" or "Ignore Limit For Today", and may ask for the Screen Time passcode. '
          'Leave the passcode to the user; do not guess it.',
  'keywords': ['downtime', 'app limit', 'screen time passcode'],
  'screens': ['alert'],
  'source': ['get-started-with-screen-time-iphbfa595995']},
 {'id': 'alert.testflight',
  'topic': 'alert',
  'title': 'TestFlight screens',
  'ios': [18, 26],
  'text': 'TestFlight shows an invitation with "Install" or "Update", then "Open" once installed, and a "What to '
          'Test" or what\'s-new note. Installing a beta is consequential; "Open" launches it. Tap "Accept" only for '
          'invites the user asked for.',
  'keywords': ['beta app', 'testflight install', 'what to test'],
  'screens': ['alert'],
  'source': ['sign-in-and-manage-your-apple-account-iph76e54c61e']},
 {'id': 'alert.trust_computer',
  'topic': 'alert',
  'title': 'Trust This Computer',
  'ios': [18, 26],
  'text': 'Title: "Trust This Computer?". Buttons: "Trust" and "Don\'t Trust"; Trust then asks for the passcode. It '
          'lets a computer read the phone. Never answer for the user; "Don\'t Trust" is the safe default.',
  'keywords': ['usb', 'connect computer', 'trust'],
  'screens': ['alert'],
  'source': ['update-ios-iph3e504502']},
 {'id': 'home.action_button.settings',
  'topic': 'home',
  'title': 'Set the Action button',
  'ios': [18, 26],
  'text': 'Only some iPhones have an Action button (iPhone 15 Pro and newer Pro, 16 and 17 families, including 16e) '
          'in place of the Ring/Silent switch. Settings › "Action Button", then swipe through actions: "Silent '
          'Mode", "Focus", "Camera", "Flashlight", "Shortcut" and others. "Controls", "Shortcut" and "Accessibility" '
          'need a specific choice picked.',
  'keywords': ['action button', 'side switch', 'silent mode', 'assign shortcut', 'iphone 15 pro'],
  'screens': ['com.apple.Preferences'],
  'source': ['use-and-customize-the-action-button-iphe89d61d66']},
 {'id': 'home.action_button.use',
  'topic': 'home',
  'title': 'Using the Action button',
  'ios': [18, 26],
  'text': 'Press and hold the Action button to run its assigned action. For Silent Mode it toggles silent on and off '
          'each press. It is a hardware button, so a screen-only agent cannot press it; to run the same shortcut, '
          'search its name in Spotlight or use the Shortcuts app.',
  'keywords': ['action button press', 'hold side button', 'run action'],
  'screens': ['com.apple.springboard'],
  'source': ['use-and-customize-the-action-button-iphe89d61d66']},
 {'id': 'home.app_library.categories',
  'topic': 'home',
  'title': 'App Library categories cannot be edited',
  'ios': [18, 26],
  'text': 'App Library groups apps into categories automatically from how you use them; you cannot move an app to '
          'another category. Touch and hold an app and drag it to move it onto the Home Screen. Deleting from here '
          'uses "Delete App" in the touch and hold menu.',
  'keywords': ['category', 'move to home screen', 'add to home screen'],
  'screens': ['com.apple.springboard'],
  'source': ['find-and-use-your-apps-in-app-library-iph87abad19a']},
 {'id': 'home.app_library.hidden',
  'topic': 'home',
  'title': 'Hidden apps live in the Hidden folder',
  'ios': [18, 26],
  'text': 'An app hidden with "Hide and Require Face ID" disappears from the Home Screen and Spotlight and sits in '
          'the "Hidden" folder at the bottom of App Library. Tap the folder and authenticate with Face ID (or Touch '
          'ID or passcode), then tap the app. Do not try to authenticate for the user.',
  'keywords': ['hide app', 'hidden folder', 'face id app', 'require face id', 'locked app'],
  'screens': ['com.apple.springboard'],
  'source': ['lock-or-hide-or-an-app-iph00f208d05']},
 {'id': 'home.app_library.new_apps',
  'topic': 'home',
  'title': 'Where new apps land',
  'ios': [26, 26],
  'text': 'New downloads go to the Home Screen and App Library, or to App Library only, depending on Settings › Home '
          'Screen & App Library › "Add to Home Screen" / "App Library Only". With hidden pages, a new app may appear '
          'only in App Library. Icons you moved there can be dragged back to the Home Screen.',
  'keywords': ['new app missing', 'downloaded app', 'install location'],
  'screens': ['com.apple.springboard'],
  'source': ['find-and-use-your-apps-in-app-library-iph87abad19a']},
 {'id': 'home.app_library.open',
  'topic': 'home',
  'title': 'Open App Library',
  'ios': [18, 26],
  'text': 'From the Home Screen swipe left past the last page. Apps appear in category folders such as "Suggestions" '
          'and "Recently Added" (plus names like "Utilities"). Small icon groups in a category expand when tapped. '
          'Tap an app to open it. Swipe up from the bottom edge to leave.',
  'keywords': ['app library', 'categories', 'suggestions', 'recently added', 'utilities', 'all apps'],
  'screens': ['com.apple.springboard'],
  'source': ['find-and-use-your-apps-in-app-library-iph87abad19a', 'open-apps-iphca3d8b4e3']},
 {'id': 'home.app_library.search',
  'topic': 'home',
  'title': 'Find an app in the App Library',
  'ios': [18, 26],
  'text': 'At the top of the App Library tap the search field (it reads "App Library"). It lists every installed app '
          'A to Z, with "Cancel" beside the field; type the app\'s name and tap its row. Apps hidden from Spotlight, '
          'on hidden Home Screen pages or with no Home Screen icon are all listed here.',
  'keywords': ['app library', 'find app', 'hidden app', 'missing app', 'all apps', 'open app'],
  'screens': ['com.apple.springboard'],
  'source': ['find-and-use-your-apps-in-app-library-iph87abad19a']},
 {'id': 'home.app_lock',
  'topic': 'home',
  'title': 'Locked apps ask for Face ID on open',
  'ios': [18, 26],
  'text': 'A locked app ("Require Face ID" in its touch and hold menu) stays on the Home Screen but needs Face ID, '
          'Touch ID or the passcode each time it opens, and relocks when you leave it. Its content is kept out of '
          'search and notification previews. Calculator, Camera, Clock, Contacts, Find My, Maps, Shortcuts and '
          'Settings cannot be locked.',
  'keywords': ['require face id', 'authentication prompt', 'app locked', 'passcode'],
  'screens': ['com.apple.springboard'],
  'source': ['lock-or-hide-or-an-app-iph00f208d05']},
 {'id': 'home.app_switcher',
  'topic': 'home',
  'title': 'Open the App Switcher and close an app',
  'ios': [18, 26],
  'text': 'Swipe up from the bottom edge and pause in the center until app cards appear (Home button: double-press). '
          'Swipe sideways to browse, tap a card to open that app. To quit an app, swipe its card up. Quitting rarely '
          'helps and loses app state; do it only for a stuck app.',
  'keywords': ['recent apps', 'multitask', 'close app', 'force quit', 'quit app', 'kill app'],
  'screens': ['com.apple.springboard'],
  'source': ['switch-between-open-apps-iph1a1f981ad',
             'quit-and-reopen-an-app-iph83bfec492',
             'learn-gestures-for-iphone-models-with-face-id-iphfdf164cac']},
 {'id': 'home.back.breadcrumb',
  'topic': 'home',
  'title': 'Return with "Back to <App>"',
  'ios': [18, 26],
  'text': 'After one app opens another, a small "Back to <App>" pill appears at the top left of the status bar. Tap '
          'it to return to the first app. ',
  'keywords': ['back to app', 'return to previous app', 'status bar link', 'breadcrumb'],
  'screens': ['any'],
  'source': ['general-ios-behavior']},
 {'id': 'home.back.edge_swipe',
  'topic': 'home',
  'title': 'Go back inside an app',
  'ios': [18, 26],
  'text': 'To go back one screen in most apps, swipe right starting at the very left edge, or tap the back button at '
          'top left, shown as "<" with the previous screen\'s title. Swipe starting in the middle of the screen '
          'scrolls or pages instead, so start at the edge. Not every app supports the edge swipe.',
  'keywords': ['back', 'previous screen', 'navigation back', 'chevron', 'left edge'],
  'screens': ['any'],
  'source': ['general-ios-behavior']},
 {'id': 'home.customize.icons.ios18',
  'topic': 'home',
  'title': 'Icon appearance and size labels (iOS 18)',
  'ios': [18, 18],
  'text': 'In edit mode tap "Edit" then "Customize". Options include "Large" (icon names disappear, so match by '
          'position or search), "Dark", "Light", "Automatic" and "Tinted". Tint and dark icons are the same apps '
          'with the same names; only large icons drop their labels.',
  'keywords': ['large icons', 'tinted', 'dark icons', 'no labels', 'customize'],
  'screens': ['com.apple.springboard'],
  'source': ['customize-apps-and-widgets-on-the-home-screen-iph385473442']},
 {'id': 'home.customize.icons.ios26',
  'topic': 'home',
  'title': 'Icon appearance and size labels (iOS 26)',
  'ios': [26, 26],
  'text': 'In edit mode tap "Edit" then "Customize". Choices: a bigger-icon button (icon names disappear, so '
          'identify apps by position or search), "Dark", "Auto", "Clear" (translucent) and "Tinted", each with '
          '"Light", "Dark" or "Auto". Clear and tinted icons keep the same app names.',
  'keywords': ['large icons', 'clear icons', 'liquid glass', 'tinted', 'no labels'],
  'screens': ['com.apple.springboard'],
  'source': ['customize-apps-and-widgets-on-the-home-screen-iph385473442']},
 {'id': 'home.dock',
  'topic': 'home',
  'title': 'The Dock',
  'ios': [18, 26],
  'text': 'The Dock is the fixed row of app icons at the bottom of every Home Screen page. It stays when you swipe '
          'between pages. On Face ID iPhones in iOS 18 and 26 a "Search" button sits just above it, between the page '
          'dots and the Dock. Dock apps open with one tap.',
  'keywords': ['dock', 'bottom row', 'favorite apps'],
  'screens': ['com.apple.springboard'],
  'source': ['move-apps-and-widgets-on-the-home-screen-iphd2fc8ce30', 'search-on-iphone-iph3c511548']},
 {'id': 'home.edit_mode.enter',
  'topic': 'home',
  'title': 'Enter Home Screen edit mode (jiggle)',
  'ios': [18, 26],
  'text': 'Touch and hold empty Home Screen background until icons jiggle. An "Edit" button appears at the top and '
          '"Done" ends editing. While jiggling, tapping an app does not open it. Leave by tapping "Done" or the '
          'background (Home button: press it).',
  'keywords': ['jiggle', 'rearrange', 'wiggle', 'edit home screen', 'done'],
  'screens': ['com.apple.springboard'],
  'source': ['move-apps-and-widgets-on-the-home-screen-iphd2fc8ce30',
             'customize-apps-and-widgets-on-the-home-screen-iph385473442']},
 {'id': 'home.edit_mode.move',
  'topic': 'home',
  'title': 'Move an app or widget to another page',
  'ios': [18, 26],
  'text': 'In edit mode, drag the icon to the right screen edge and wait a moment: the next page appears and you '
          'drop it there. Dropping an icon on another icon makes a folder. The dots above the Dock show the pages. '
          'Layouts differ per page.',
  'keywords': ['move app', 'drag icon', 'rearrange apps', 'new page'],
  'screens': ['com.apple.springboard'],
  'source': ['move-apps-and-widgets-on-the-home-screen-iphd2fc8ce30']},
 {'id': 'home.folders',
  'topic': 'home',
  'title': 'Folders on the Home Screen',
  'ios': [18, 26],
  'text': 'Tap a folder to open it; it can hold several pages, swipe sideways inside it. Tap outside to close. To '
          'make one in edit mode, drag an app onto another app. To rename: touch and hold the folder, tap "Rename". '
          'Emptying a folder deletes it. Folders do not change App Library categories.',
  'keywords': ['folder', 'group apps', 'rename folder', 'create folder'],
  'screens': ['com.apple.springboard'],
  'source': ['organize-your-apps-in-folders-iph822ece7dd']},
 {'id': 'home.go_home',
  'topic': 'home',
  'title': 'Go to the Home Screen',
  'ios': [18, 26],
  'text': 'Swipe up from the bottom edge to go home from any app (Home button: press it). Pressing Home again on the '
          'Home Screen returns to its first page. Swipe left or right to change pages; the dots above the Dock show '
          'how many pages there are and which one is showing.',
  'keywords': ['home', 'go home', 'swipe up', 'exit app', 'first page'],
  'screens': ['com.apple.springboard'],
  'source': ['open-apps-iphca3d8b4e3', 'learn-gestures-for-iphone-models-with-face-id-iphfdf164cac']},
 {'id': 'home.hidden_pages',
  'topic': 'home',
  'title': 'Hide or show Home Screen pages',
  'ios': [18, 26],
  'text': 'In edit mode, tap the page dots. Thumbnails of each page appear with checkmarks; tap a thumbnail to hide '
          'it (checkmark gone) or show it again, drag thumbnails to reorder, then tap "Done". Hidden pages are '
          'unreachable by swiping, so apps on them are found via App Library or search.',
  'keywords': ['edit pages', 'hidden pages', 'page thumbnails', 'reorder pages', 'missing apps'],
  'screens': ['com.apple.springboard'],
  'source': ['find-and-use-your-apps-in-app-library-iph87abad19a']},
 {'id': 'home.page_control',
  'topic': 'home',
  'title': 'Which Home Screen page is showing',
  'ios': [26, 26],
  'text': 'The Home Screen\'s page dots read "Page 1 of 3" to accessibility, and a "Search" button sits on them '
          'above the Dock: tap it for Spotlight. After the last page, one more swipe left shows the App Library.',
  'keywords': ['page dots', 'home page', 'which page', 'search button', 'dock'],
  'screens': ['com.apple.springboard'],
  'source': ['recorded: iOS 26.4 simulator Home Screen, 14 Oct 2026']},
 {'id': 'home.pages.swipe',
  'topic': 'home',
  'title': 'Swipe between Home Screen pages',
  'ios': [18, 26],
  'text': 'Swipe left to see later Home Screen pages and right to go back. Swiping left past the last page opens App '
          'Library. The dots above the Dock show how many pages exist and which one is showing. In edit mode, '
          'tapping the dots opens the page editor.',
  'keywords': ['pages', 'page dots', 'next page', 'scroll home'],
  'screens': ['com.apple.springboard'],
  'source': ['open-apps-iphca3d8b4e3', 'move-apps-and-widgets-on-the-home-screen-iphd2fc8ce30']},
 {'id': 'home.reachability',
  'topic': 'home',
  'title': 'Reachability',
  'ios': [18, 26],
  'text': 'Settings › Accessibility › Touch › "Reachability" must be on. Then swipe down on the bottom edge of the '
          'screen (Home button: lightly double-tap Home) to slide the top half down. Tap the upper half or wait to '
          'restore the full screen.',
  'keywords': ['one hand', 'lower screen', 'reach top', 'pull screen down'],
  'screens': ['com.apple.Preferences', 'com.apple.springboard'],
  'source': ['use-reachability-iph145eba8e9']},
 {'id': 'home.remove_app',
  'topic': 'home',
  'title': 'Remove or delete an app (consequential)',
  'ios': [18, 26],
  'text': 'Touch and hold an app icon, tap "Remove App", then choose "Remove from Home Screen" (the app stays in App '
          'Library) or "Delete App" (the app and its data go; confirm with "Delete"). In App Library the menu offers '
          '"Delete App". Treat Delete App as destructive and ask before tapping it.',
  'keywords': ['delete app', 'uninstall', 'remove from home screen', 'minus badge', 'irreversible'],
  'screens': ['com.apple.springboard'],
  'source': ['remove-or-delete-apps-iph248b543ca', 'move-apps-and-widgets-on-the-home-screen-iphd2fc8ce30']},
 {'id': 'home.reset_layout',
  'topic': 'home',
  'title': 'Reset Home Screen layout',
  'ios': [18, 26],
  'text': 'Settings › General › Transfer or Reset iPhone › Reset › "Reset Home Screen Layout" › "Reset Home Screen". '
          'It removes folders and puts downloaded apps in alphabetical order after the built-in apps. Consequential: '
          'it rearranges everything, so ask first.',
  'keywords': ['reset layout', 'lost apps', 'home screen mess'],
  'screens': ['com.apple.Preferences'],
  'source': ['move-apps-and-widgets-on-the-home-screen-iphd2fc8ce30']},
 {'id': 'home.shortcuts_app',
  'topic': 'home',
  'title': 'Shortcuts app',
  'ios': [18, 26],
  'text': 'The Shortcuts app runs automations with a tap or by asking Siri; tapping a shortcut tile runs it. '
          'Shortcuts also appear in Spotlight when you search their name or an app name. Shortcuts cannot be locked '
          'with Face ID, and the Shortcuts app can be deleted.',
  'keywords': ['shortcuts', 'automation', 'gallery', 'run shortcut'],
  'screens': ['com.apple.springboard'],
  'source': ['shortcuts-iph47e1c9d7d', 'lock-or-hide-or-an-app-iph00f208d05']},
 {'id': 'home.siri.invoke',
  'topic': 'home',
  'title': 'Invoke Siri',
  'ios': [18, 26],
  'text': 'Press and hold the side button (Home button: hold the Home button), or say "Siri" or "Hey Siri". Pressing '
          'the side button needs "Press Side Button for Siri" in Settings › Siri (or "Apple Intelligence & Siri") › '
          '"Talk to Siri". Siri needs the internet. Spoken input is unavailable to a UI-only agent; use typing.',
  'keywords': ['siri', 'voice assistant', 'hey siri', 'side button', 'talk to siri'],
  'screens': ['com.apple.springboard'],
  'source': ['turn-on-and-activate-siri-iph83aad8922', 'learn-gestures-for-iphone-models-with-face-id-iphfdf164cac']},
 {'id': 'home.siri.type',
  'topic': 'home',
  'title': 'Type to Siri',
  'ios': [18, 26],
  'text': 'Settings › Accessibility › Siri › "Type to Siri" lets you type requests after Siri opens. With Apple '
          'Intelligence on, Siri has a keyboard built in: activate it, then type in the text field. Settings › Siri '
          'has "Talk to Siri" (or "Talk & Type to Siri") to turn listening off.',
  'keywords': ['type siri', 'keyboard siri', 'text to siri', 'siri without voice'],
  'screens': ['com.apple.Preferences', 'com.apple.springboard'],
  'source': ['turn-on-and-activate-siri-iph83aad8922']},
 {'id': 'home.spotlight.app_toggle',
  'topic': 'home',
  'title': 'Control whether an app appears in Spotlight',
  'ios': [18, 26],
  'text': 'Settings › Search, scroll down, tap the app, then turn "Show App in Search" on or off. With it off, '
          'Spotlight no longer finds that app by name, but the app is still in App Library and on the Home Screen. '
          'Also see "Show Content in Search" on the same screen where present.',
  'keywords': ['show app in search', 'show content in search', 'app missing in search', 'exclude app'],
  'screens': ['com.apple.Preferences'],
  'source': ['search-on-iphone-iph3c511548']},
 {'id': 'home.spotlight.go',
  'topic': 'home',
  'title': 'Submit a Spotlight search',
  'ios': [18, 18],
  'text': 'After typing, tap "Search" or "Go" on the keyboard to run the query; tapping a suggested app or website '
          'opens it directly. Tap the clear (x) in the field to start over.',
  'keywords': ['go key', 'return key', 'submit search', 'cancel search', 'clear search'],
  'screens': ['com.apple.springboard'],
  'source': ['search-on-iphone-iph3c511548']},
 {'id': 'home.spotlight.go.ios26',
  'topic': 'home',
  'title': 'Submit a Spotlight search (iOS 26)',
  'ios': [26, 26],
  'text': 'After typing, tap the search key on the keyboard to run the query; tapping a suggested app or website '
          'opens it directly. Tap the clear (x) in the field to start over.',
  'keywords': ['return key', 'submit search', 'cancel search', 'clear search'],
  'screens': ['com.apple.springboard'],
  'source': ['search-on-iphone-iph3c511548']},
 {'id': 'home.spotlight.hidden_app',
  'topic': 'home',
  'title': "When Spotlight doesn't find an app",
  'ios': [18, 26],
  'text': 'Spotlight lists an app under "Top Hit" with its icon, and the field shows "<App> — Open". If its results '
          'show only web, dictionary, Siri Knowledge or "Search in Apps" rows, the app is hidden from search '
          '(Settings › Search › the app › "Show App in Search" is off): don\'t press the search key, which searches '
          'the web. Open it from the App Library instead.',
  'keywords': ['spotlight', 'app not found', 'open app', 'top hit', 'hidden from search', 'web results'],
  'screens': ['com.apple.springboard', 'com.apple.Spotlight'],
  'source': ['recorded: iOS 26.4 simulator Spotlight; a real iPhone on iOS 26.0.1, Settings › Search, 14 Oct 2026']},
 {'id': 'home.spotlight.open',
  'topic': 'home',
  'title': 'Open Spotlight search',
  'ios': [18, 26],
  'text': 'Swipe down from the middle of the Home Screen, or tap the "Search" button above the Dock (Face ID '
          'iPhones). Swiping down on the Lock Screen also opens search. Type in the search field; results update as '
          'you type.',
  'keywords': ['spotlight', 'search', 'find anything', 'swipe down', 'search bar'],
  'screens': ['com.apple.springboard'],
  'source': ['search-on-iphone-iph3c511548']},
 {'id': 'home.spotlight.results',
  'topic': 'home',
  'title': 'Read and use Spotlight results',
  'ios': [18, 26],
  'text': 'Results start with a "Top Hit" (searching an app name also lists its shortcuts), then apps, contacts, '
          'in-app content, suggested websites and web results. Tap a result to open it. Searching an app name and '
          'tapping it launches the app, which is the fastest way to open any installed app.',
  'keywords': ['top hit', 'search results', 'launch app', 'search in app', 'web results'],
  'screens': ['com.apple.springboard'],
  'source': ['search-on-iphone-iph3c511548']},
 {'id': 'home.spotlight.shortcuts',
  'topic': 'home',
  'title': 'App shortcuts in Spotlight',
  'ios': [18, 26],
  'text': "Search an app's name and shortcuts for likely next actions appear in the Top Hit, such as a Favorites "
          'album for Photos. Spotlight can also start a timer, turn on a Focus, or run any shortcut by name; '
          'shortcuts come from the Shortcuts app.',
  'keywords': ['app shortcuts', 'quick action', 'run shortcut', 'start timer'],
  'screens': ['com.apple.springboard'],
  'source': ['search-on-iphone-iph3c511548']},
 {'id': 'home.spotlight.siri_suggestions',
  'topic': 'home',
  'title': 'Siri Suggestions settings',
  'ios': [18, 26],
  'text': 'Settings › Siri (or "Apple Intelligence & Siri") › Apps lets you pick an app and change its suggestion '
          'switches. The main Siri screen has "Show in App", "Show on Home Screen", "Suggest App" and "Suggestion '
          'Notifications". Turning these off removes suggested items, not the apps.',
  'keywords': ['siri suggestions', 'suggested app', 'suggestions in search'],
  'screens': ['com.apple.Preferences'],
  'source': ['turn-siri-suggestions-on-or-off-iph6f94af287']},
 {'id': 'home.switch_recent',
  'topic': 'home',
  'title': 'Quick switch to the previous app',
  'ios': [18, 26],
  'text': 'On Face ID iPhones, swipe right or left along the bottom edge of the screen to jump to the neighboring '
          'recent app without opening the App Switcher. Swiping right goes to the previous app.',
  'keywords': ['previous app', 'last app', 'flick bottom edge', 'switch apps'],
  'screens': ['com.apple.springboard'],
  'source': ['switch-between-open-apps-iph1a1f981ad', 'learn-gestures-for-iphone-models-with-face-id-iphfdf164cac']},
 {'id': 'keyboard.autocorrect',
  'topic': 'keyboard',
  'title': 'Autocorrect can change typed words',
  'ios': [18, 26],
  'text': 'iOS corrects misspelled words and underlines the change. Check the field after typing names, codes and '
          'usernames. To keep your spelling, tap the underlined word, then tap your original text in the suggestion. '
          'Turn it off in Settings › General › Keyboard › "Auto-Correction". Predictive text suggestions appear in a '
          'bar above the keys.',
  'keywords': ['predictive text', 'spell check', 'wrong word', 'suggestion bar'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.dictation',
  'topic': 'keyboard',
  'title': 'Dictate with the microphone',
  'ios': [18, 26],
  'text': 'Tap the microphone key on the keyboard, or the microphone in the field, then speak; tap it again or say '
          '"Stop dictation" to end. Say "new line" or "new paragraph" for breaks, and punctuation names. It stops '
          'after 30 seconds of silence. It needs Settings › General › Keyboard › "Enable Dictation". Typing stays '
          'available.',
  'keywords': ['voice typing', 'speak', 'microphone key', 'speech to text'],
  'screens': ['keyboard'],
  'source': ['dictate-text-iph2c0651d2']},
 {'id': 'keyboard.dictation_commands',
  'topic': 'keyboard',
  'title': 'Dictation edit commands',
  'ios': [26, 26],
  'text': 'While dictating, say "select" or "delete" plus a phrase to change text, "undo" or "redo" to reverse a '
          'command, and an emoji\'s name such as "heart emoji" to insert it. Spell a word by letters. Typing by hand '
          'is more reliable for exact strings such as emails.',
  'keywords': ['voice commands', 'delete phrase', 'say emoji'],
  'screens': ['keyboard'],
  'source': ['dictate-text-iph2c0651d2']},
 {'id': 'keyboard.dismiss',
  'topic': 'keyboard',
  'title': 'Dismiss the keyboard',
  'ios': [18, 26],
  'text': 'No universal dismiss key exists. Try in order: the "Done" key or toolbar button, a down-chevron key where '
          'present, tapping outside the field, or swiping down on a scrolling list. In some chat fields "return" '
          'adds a line and never closes it. Dragging down on the keyboard works in apps that support it.',
  'keywords': ['hide keyboard', 'close keyboard', 'keyboard in the way', 'done button'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.number_symbol_keys',
  'topic': 'keyboard',
  'title': 'Numbers and symbols: 123 and ABC',
  'ios': [18, 26],
  'text': 'Tap "123" for numbers and punctuation, then "#+=" for more symbols. Tap "ABC" to return to letters. Touch '
          'and hold a key such as e, a, or a punctuation mark and slide to pick a variant (é, accents). Double-tap '
          'the space bar to end a sentence with a period and a space.',
  'keywords': ['numbers', 'symbols', 'punctuation', 'accented letters', 'period'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.one_handed',
  'topic': 'keyboard',
  'title': 'One-handed keyboard',
  'ios': [18, 26],
  'text': 'Touch and hold the globe or emoji key and tap a keyboard layout to push the keys to the left or right '
          'edge. Tap the opposite edge arrow to center it. The keys then sit at a different position, so re-read the '
          'screen before tapping.',
  'keywords': ['thumb keyboard', 'shift keyboard', 'keyboard layout'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.paste_key',
  'topic': 'keyboard',
  'title': 'Paste key and clipboard prompt',
  'ios': [18, 26],
  'text': 'Touch the field, then tap "Paste" in the small menu that appears, or the "Paste" key on the keyboard bar '
          'when shown. Pasting from another app may raise "Allow Paste"; see the system sheet entry. If the '
          'clipboard is empty, "Paste" does not appear.',
  'keywords': ['clipboard', 'paste text', 'paste button'],
  'screens': ['keyboard'],
  'source': ['select-and-edit-text-iph1a9cae52c']},
 {'id': 'keyboard.return_key.labels',
  'topic': 'keyboard',
  'title': "Read the return key's label",
  'ios': [18, 26],
  'text': 'The key at the bottom right names what it does in that field: "return" adds a new line, "Go", "Search", '
          '"Send", "Next", "Join" or "Done" submit or advance. Read the label before tapping. "Send" and "Join" act '
          'at once (consequential); "Next" moves to the next field; "Done" ends editing.',
  'keywords': ['enter key', 'submit', 'go key', 'search key', 'send key'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.settings_path',
  'topic': 'keyboard',
  'title': 'Keyboard settings',
  'ios': [18, 26],
  'text': 'Settings › General › Keyboard holds "Auto-Correction", "Enable Dictation", "Auto-Punctuation", '
          '"Keyboards" and the one-handed layouts. While typing, touch and hold the globe or emoji key and tap '
          '"Keyboard Settings" to jump there. Settings › Sounds & Haptics › "Keyboard Feedback" toggles click sounds '
          'and haptics.',
  'keywords': ['turn off autocorrect', 'keyboard sound', 'predictive'],
  'screens': ['keyboard', 'com.apple.Preferences'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e', 'add-or-change-keyboards-iph73b71eb']},
 {'id': 'keyboard.shift_caps',
  'topic': 'keyboard',
  'title': 'Shift and Caps Lock',
  'ios': [18, 26],
  'text': 'Tap Shift (up arrow) for one capital letter. Double-tap Shift for Caps Lock; the arrow turns solid or '
          'underlined. Tap Shift again to turn it off. You can also touch Shift and slide to a letter. Read the key '
          'state before typing capitals.',
  'keywords': ['uppercase', 'capital letters', 'caps lock', 'shift key'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.space_trackpad',
  'topic': 'keyboard',
  'title': 'Move the cursor with the space bar',
  'ios': [18, 26],
  'text': 'Touch and hold the space bar until the keys go blank, then slide to move the insertion point. Touch and '
          'hold with a second finger to select text, adjusting by moving the first finger. Use this for precise '
          'cursor placement when tapping lands in the wrong spot.',
  'keywords': ['trackpad', 'move cursor', 'caret', 'cursor position'],
  'screens': ['keyboard'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e', 'select-and-edit-text-iph1a9cae52c']},
 {'id': 'keyboard.switch_keyboards',
  'topic': 'keyboard',
  'title': 'Switch keyboards and emoji',
  'ios': [18, 26],
  'text': 'Tap the globe key to cycle keyboards or open emoji; touch and hold it to pick one from a list. Without a '
          'globe key, touch and hold the emoji smiley key. Add or remove keyboards in Settings › General › Keyboard '
          '› "Keyboards" › "Add New Keyboard".',
  'keywords': ['globe key', 'emoji keyboard', 'language keyboard', 'change language'],
  'screens': ['keyboard'],
  'source': ['add-or-change-keyboards-iph73b71eb', 'type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'keyboard.undo_shake',
  'topic': 'keyboard',
  'title': 'Undo typing and redo',
  'ios': [18, 26],
  'text': 'Swipe left with three fingers, then tap "Undo" at the top; swipe right for "Redo". Or shake the iPhone, '
          'which raises an "Undo Typing" alert with "Undo" and "Cancel" buttons. Shake can fire by accident; '
          '"Cancel" keeps the text.',
  'keywords': ['shake to undo', 'undo typing', 'three finger swipe', 'redo'],
  'screens': ['keyboard', 'alert'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'panels.control_center.controls',
  'topic': 'panels',
  'title': 'Default Control Center controls',
  'ios': [18, 26],
  'text': 'The top-left group holds Airplane Mode, Cellular Data, Wi-Fi and Bluetooth buttons (plus AirDrop on long '
          'press). Other default controls: Now Playing, Focus, Screen Mirroring, Orientation Lock, Brightness '
          'slider, Volume slider, Flashlight, Timer, Calculator, Camera. Controls vary because users can add or '
          'remove them. Tap a toggle to switch it; touch and hold a control for more options.',
  'keywords': ['airplane mode', 'orientation lock', 'screen mirroring', 'brightness', 'flashlight button', 'toggle'],
  'screens': ['com.apple.springboard'],
  'source': ['use-and-customize-control-center-iph59095ec58']},
 {'id': 'panels.control_center.edit.ios18',
  'topic': 'panels',
  'title': 'Customize Control Center',
  'ios': [18, 18],
  'text': 'Open Control Center, tap "+" at the top left. Drag a control to move it, tap the minus at its upper left '
          'to remove it, drag its lower-right handle to resize. Tap "Add a Control" at the bottom to open the '
          'gallery and tap a control to add it. For a new group, tap the small circle on the right edge. ',
  'keywords': ['edit control center', 'add control', 'remove control', 'rearrange', 'plus button', 'gallery'],
  'screens': ['com.apple.springboard'],
  'source': ['use-and-customize-control-center-iph59095ec58']},
 {'id': 'panels.control_center.edit.ios26',
  'topic': 'panels',
  'title': 'Customize Control Center',
  'ios': [26, 26],
  'text': 'Open Control Center, tap the "+" at the top left to edit. Drag a control to move it, tap the minus at its '
          'upper left to remove it, drag its lower-right handle to resize. Tap "Add a Control" at the bottom, then '
          'pick from the gallery. For a new section, tap the small circle on the right edge, or drag a control off '
          'the bottom of the page. ',
  'keywords': ['edit control center', 'add control', 'remove control', 'rearrange', 'new section', 'gallery'],
  'screens': ['com.apple.springboard'],
  'source': ['use-and-customize-control-center-iph59095ec58']},
 {'id': 'panels.control_center.in_apps',
  'topic': 'panels',
  'title': 'Control Center unavailable inside apps',
  'ios': [18, 26],
  'text': 'If swiping down from the top-right corner inside an app does nothing, Control Center access in apps is '
          'off. Go to Settings › Control Center and turn on "Access Within Apps". The Lock Screen switch lives at '
          'Settings › Face ID & Passcode (Touch ID & Passcode on Home button models) under "Allow Access When '
          'Locked".',
  'keywords': ['control center not opening', 'access within apps', 'locked', 'disabled'],
  'screens': ['com.apple.Preferences', 'com.apple.springboard'],
  'source': ['use-and-customize-control-center-iph59095ec58', 'turn-on-lock-screen-features-iph9a2a69136']},
 {'id': 'panels.control_center.long_press',
  'topic': 'panels',
  'title': 'Touch and hold a Control Center control',
  'ios': [18, 26],
  'text': 'Touch and hold (long press) a control to open its options: the top-left connectivity group expands to '
          'show AirDrop, the brightness and volume sliders enlarge, Focus lists its modes, and Camera offers selfie, '
          'video and photo. Touch and hold the power button at the top right until a vibration, then drag the slider '
          'to power off. A plain tap just toggles the control.',
  'keywords': ['long press', 'hold', 'more options', 'power off', 'AirDrop', 'expand'],
  'screens': ['com.apple.springboard'],
  'source': ['use-and-customize-control-center-iph59095ec58']},
 {'id': 'panels.control_center.open',
  'topic': 'panels',
  'title': 'Open and close Control Center',
  'ios': [18, 26],
  'text': 'Swipe down from the top-right corner (Home button: swipe up from the bottom edge). Control Center works '
          'over any app and on the Lock Screen. Keep swiping down to reach more groups of controls, or tap the small '
          'icons on the right edge to jump between groups. Close it by swiping up from the bottom center (Home '
          'button: press Home). Swiping down from the top center opens Notification Center instead.',
  'keywords': ['quick settings', 'control panel', 'swipe down corner', 'close control center', 'shade'],
  'screens': ['com.apple.springboard'],
  'source': ['use-and-customize-control-center-iph59095ec58']},
 {'id': 'panels.control_center.wifi_bluetooth',
  'topic': 'panels',
  'title': 'Wi-Fi and Bluetooth buttons only disconnect',
  'ios': [18, 26],
  'text': 'Tapping the Wi-Fi or Bluetooth button in Control Center only disconnects you for now. Wi-Fi and Bluetooth '
          'stay on, AirDrop and AirPlay still work, and iPhone rejoins known networks when you move or restart. '
          'Touch and hold the Wi-Fi button to see the network name. To truly turn them off, use Settings › Wi-Fi or '
          'Settings › Bluetooth and switch them off there.',
  'keywords': ['turn off wifi', 'turn off bluetooth', 'disconnect', 'still on', 'gray button'],
  'screens': ['com.apple.springboard', 'com.apple.Preferences'],
  'source': ['use-and-customize-control-center-iph59095ec58']},
 {'id': 'panels.flashlight',
  'topic': 'panels',
  'title': 'Turn the flashlight on or off',
  'ios': [18, 26],
  'text': 'Open Control Center and tap the flashlight button. On the Lock Screen, press the flashlight button at the '
          "bottom left. Tap it again to turn off. After it's on, drag up or down on the flashlight image to change "
          'brightness. If the button is missing in Control Center, add it with the "+" edit mode. An Action button '
          'can also be set to toggle it.',
  'keywords': ['torch', 'light', 'flash', 'brightness of flashlight'],
  'screens': ['com.apple.springboard'],
  'source': ['turn-the-iphone-flashlight-on-or-off-iphf574afb44']},
 {'id': 'panels.focus.settings',
  'topic': 'panels',
  'title': 'Focus settings and schedules',
  'ios': [18, 26],
  'text': 'Settings › Focus lists every Focus. Tap one to set "People" and "Apps" allowed to notify you, "Options" '
          'for Lock Screen behavior, and "Add Schedule" to turn it on at a time, place or while using an app. A '
          'Focus can also be turned on or off from this screen. To remove one, open it, scroll down and tap "Delete '
          'Focus".',
  'keywords': ['focus schedule', 'allowed people', 'allowed apps', 'delete focus', 'automatic focus'],
  'screens': ['com.apple.Preferences'],
  'source': ['set-up-a-focus-iphd6288a67f', 'turn-a-focus-on-or-off-iph5c3f5b77b']},
 {'id': 'panels.focus.turn_on',
  'topic': 'panels',
  'title': 'Turn a Focus on or off',
  'ios': [18, 26],
  'text': 'Open Control Center, tap "Focus", then tap a Focus such as "Do Not Disturb". Tapping the one that is on '
          'turns it off, and tapping another switches to it. Tap the ellipsis (...) next to it to choose a duration, '
          'e.g. "For 1 hour" or "Until I leave this location". You can also touch and hold the Focus icon on the '
          'Lock Screen to turn it off.',
  'keywords': ['enable do not disturb', 'silence phone', 'mute notifications', 'moon icon', 'duration'],
  'screens': ['com.apple.springboard'],
  'source': ['turn-a-focus-on-or-off-iph5c3f5b77b']},
 {'id': 'panels.focus.what',
  'topic': 'panels',
  'title': 'What Focus is',
  'ios': [18, 26],
  'text': 'Focus silences notifications and calls except the people and apps you allow, e.g. "Do Not Disturb", '
          '"Sleep", "Work", "Personal". While one is on, its icon appears in the status bar and Lock Screen, and '
          "Messages tells senders you've silenced notifications (they can still notify you if urgent). Silenced "
          'notifications are not lost; read them later in Notification Center.',
  'keywords': ['do not disturb', 'dnd', 'silence', 'sleep focus', 'work focus', 'quiet'],
  'screens': ['com.apple.springboard', 'com.apple.Preferences'],
  'source': ['turn-a-focus-on-or-off-iph5c3f5b77b', 'set-up-a-focus-iphd6288a67f']},
 {'id': 'panels.lock_screen.gestures',
  'topic': 'panels',
  'title': 'Lock Screen swipes',
  'ios': [18, 26],
  'text': 'From the Lock Screen: swipe left for Camera; swipe right for more widgets (Today view); swipe down from '
          'the top-right corner for Control Center; swipe up from the middle to see earlier notifications; swipe up '
          'from the bottom edge to unlock (Face ID) or go to the passcode screen. Tap or press the camera or '
          'flashlight button to use it without unlocking.',
  'keywords': ['swipe left camera', 'swipe right widgets', 'today view', 'lock screen camera'],
  'screens': ['com.apple.springboard'],
  'source': ['access-features-from-the-lock-screen-iphcd5c65ccf']},
 {'id': 'panels.lock_screen.layout',
  'topic': 'panels',
  'title': 'What the Lock Screen shows',
  'ios': [18, 26],
  'text': 'The Lock Screen shows the time and date, optional widgets, notifications, Now Playing controls, Live '
          'Activities, a flashlight button at the bottom left and a camera button at the bottom right (on models '
          'with them). It appears when you wake iPhone. Many items work while locked; reading private content or '
          'opening apps usually needs Face ID, Touch ID or the passcode.',
  'keywords': ['wake screen', 'locked screen', 'time widget', 'flashlight camera buttons', 'wallpaper'],
  'screens': ['com.apple.springboard'],
  'source': ['access-features-from-the-lock-screen-iphcd5c65ccf', 'wake-unlock-and-lock-iph5a0b5b9c5']},
 {'id': 'panels.lock_screen.live_activities',
  'topic': 'panels',
  'title': 'Live Activities and Now Playing',
  'ios': [18, 26],
  'text': 'Live Activities show ongoing app updates (directions, sports scores, delivery progress, timers) on the '
          'Lock Screen and in the Dynamic Island. Media playing shows Now Playing controls to play, pause and skip. '
          'Tap a Live Activity to open its app. Turn them off per app at Settings › Apps › the app › "Live '
          'Activities" (not every app has this switch).',
  'keywords': ['dynamic island', 'now playing', 'live activity', 'timer', 'delivery tracking', 'media controls'],
  'screens': ['com.apple.springboard', 'com.apple.Preferences'],
  'source': ['access-features-from-the-lock-screen-iphcd5c65ccf',
             'view-live-activities-in-the-dynamic-island-iph28f50d10d']},
 {'id': 'panels.lock_screen.unlock',
  'topic': 'panels',
  'title': 'Unlock iPhone',
  'ios': [18, 26],
  'text': 'Wake iPhone: side button, raise, or tap. Face ID: look at the phone until the lock icon opens, then swipe '
          'up from the bottom of the screen. Passcode: swipe up from the bottom, then type the passcode on the '
          'keypad. Home button models: press the Home button with a registered finger, or press it and enter the '
          'passcode.',
  'keywords': ['wake', 'face id', 'passcode', 'touch id', 'unlock phone', 'side button', 'enter passcode'],
  'screens': ['com.apple.springboard'],
  'source': ['wake-unlock-and-lock-iph5a0b5b9c5']},
 {'id': 'panels.low_power.ios18',
  'topic': 'panels',
  'title': 'Low Power Mode',
  'ios': [18, 18],
  'text': 'Turn it on at Settings › Battery › "Low Power Mode", or add the Low Power Mode control to Control Center '
          'and tap it. The battery icon turns yellow while it is on. It switches off by itself when the battery '
          'charges to 80% or more. At 20% and 10% iOS shows a "Low Battery" alert with "Low Power Mode" and "Close" '
          'buttons.',
  'keywords': ['battery saver', 'yellow battery', 'low battery alert', 'save power', '20 percent'],
  'screens': ['com.apple.Preferences', 'alert', 'com.apple.springboard'],
  'source': ['save-battery-life-with-power-modes-on-iphone-iphcab9aecd1']},
 {'id': 'panels.low_power.ios26',
  'topic': 'panels',
  'title': 'Low Power Mode and Adaptive Power',
  'ios': [26, 26],
  'text': 'iPhone 15 and later: Settings › Battery › "Power Mode" › "Low Power Mode" (iPhone 14 or earlier: Settings '
          '› Battery › "Low Power Mode"). Or tap its Control Center control. Battery icon turns yellow; it turns off '
          'after charging to 80%. "Adaptive Power" (same screen) adjusts performance automatically. At 20% and 10% a '
          '"Low Battery" alert offers "Low Power Mode" and "Close".',
  'keywords': ['battery saver', 'yellow battery', 'low battery alert', 'adaptive power', 'save power', '20 percent'],
  'screens': ['com.apple.Preferences', 'alert', 'com.apple.springboard'],
  'source': ['save-battery-life-with-power-modes-on-iphone-iphcab9aecd1']},
 {'id': 'panels.notification_center.actions',
  'topic': 'panels',
  'title': 'Notification actions: Options, View, Mute',
  'ios': [18, 26],
  'text': 'Touch and hold a notification to see quick actions the app offers (such as reply). Swipe left on it to '
          'get "Options" and "Clear"; tap "Options" to mute the app for an hour or a day, "Turn Off" to stop that '
          'app\'s notifications, or "View Settings". Muted notifications still land in Notification Center, '
          'silently. "Unmute" appears in the same menu.',
  'keywords': ['mute notifications',
               'turn off notifications',
               'quick reply',
               'long press notification',
               'view settings',
               'swipe left'],
  'screens': ['com.apple.springboard'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc']},
 {'id': 'panels.notification_center.clear',
  'topic': 'panels',
  'title': 'Clear notifications',
  'ios': [18, 26],
  'text': 'Swipe left on a notification or stack, then tap "Clear" or "Clear All". To clear everything in '
          'Notification Center, tap the X button above the list then tap "Clear". A banner that appears while using '
          'an app: tap to view it, or swipe it up to dismiss it. Clearing removes the notification without opening '
          'the app.',
  'keywords': ['dismiss', 'delete notification', 'clear all', 'x button', 'swipe away'],
  'screens': ['com.apple.springboard'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc']},
 {'id': 'panels.notification_center.lock_screen',
  'topic': 'panels',
  'title': 'Notifications on the Lock Screen',
  'ios': [18, 26],
  'text': 'Notifications appear on the Lock Screen while locked. Preview text is hidden unless Settings › '
          'Notifications › "Show Previews" is "Always" (or "When Unlocked", which needs Face ID or passcode). '
          'Reading full text may require unlocking first. If Notification Center is off for the locked state, enable '
          'it at Settings › Face ID & Passcode › "Allow Access When Locked" › "Notification Center".',
  'keywords': ['show previews', 'locked notifications', 'lock screen notifications', 'hidden text', 'face id'],
  'screens': ['com.apple.springboard', 'com.apple.Preferences'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc', 'access-features-from-the-lock-screen-iphcd5c65ccf']},
 {'id': 'panels.notification_center.open',
  'topic': 'panels',
  'title': 'Open and close Notification Center',
  'ios': [18, 26],
  'text': 'On any screen except the Lock Screen, swipe down from the top center (or top left) to open Notification '
          'Center, then scroll up for older notifications. On the Lock Screen, swipe up from the middle of the '
          'screen to see earlier notifications. Close it by swiping up from the bottom with one finger (Home button: '
          'press Home). Swiping from the top-right corner opens Control Center instead.',
  'keywords': ['notifications list', 'pull down', 'swipe down top', 'missed notifications', 'shade', 'history'],
  'screens': ['com.apple.springboard'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc']},
 {'id': 'panels.notification_center.scheduled_summary',
  'topic': 'panels',
  'title': 'Scheduled Summary',
  'ios': [18, 26],
  'text': "Scheduled Summary delivers chosen apps' notifications as one digest at set times instead of immediately. "
          'Set it up in Settings › Notifications › "Scheduled Summary": turn it on, set times ("Add Summary" for '
          'another), then pick apps under "Apps in Summary" ("A to Z"). Apps must have "Allow Notifications" on. '
          'Until the summary arrives, those notifications do not appear as banners.',
  'keywords': ['digest', 'batch notifications', 'summary time', 'delay notifications', 'apps in summary'],
  'screens': ['com.apple.Preferences', 'com.apple.springboard'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc']},
 {'id': 'panels.notification_center.settings',
  'topic': 'panels',
  'title': "Change one app's notification settings",
  'ios': [18, 26],
  'text': 'Go to Settings › Notifications and tap the app. "Allow Notifications" switches it on or off; choose where '
          'alerts show (Lock Screen, Notification Center, Banners) and the banner style. For quick access, swipe '
          'left on one of the app\'s notifications, tap "Options", then "View Settings". Apps you never opened may '
          "not be listed until they've asked for permission.",
  'keywords': ['allow notifications', 'banners', 'alert style', 'permission', 'app notifications'],
  'screens': ['com.apple.Preferences'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc', 'change-notification-settings-iph7c3d96bab']},
 {'id': 'panels.notification_center.stacks',
  'topic': 'panels',
  'title': 'Grouped notification stacks',
  'ios': [18, 26],
  'text': 'Notifications are grouped into stacks by app, newest on top; some apps also group by thread or topic. Tap '
          'a stack to expand it into individual notifications, and tap "Show Less" to collapse. Tap a single '
          'notification to open its app. On the Lock Screen you can pinch to switch between count, stack and list '
          'layouts.',
  'keywords': ['stack', 'expand notifications', 'show less', 'group', 'count', 'list layout'],
  'screens': ['com.apple.springboard'],
  'source': ['view-and-respond-to-notifications-iph6534c01bc', 'access-features-from-the-lock-screen-iphcd5c65ccf']},
 {'id': 'panels.notification_center.summaries_ai',
  'topic': 'panels',
  'title': 'Apple Intelligence notification summaries',
  'ios': [18, 26],
  'text': 'On iPhones with Apple Intelligence turned on, long or stacked notifications can show a short AI summary. '
          'Control it in Settings › Notifications › "Summarize Notifications", then "Choose Notifications to '
          'Summarize" per app. Not available on all models, languages or regions. The summary is a condensed '
          'description; open the notification to read the original text.',
  'keywords': ['summarize', 'AI', 'priority notifications', 'condense', 'apple intelligence'],
  'screens': ['com.apple.Preferences', 'com.apple.springboard'],
  'source': ['summarize-notifications-reduce-interruptions-iph1fbe7d2b9']},
 {'id': 'panels.screen_time.limit_reached',
  'topic': 'panels',
  'title': '"Time Limit" screen on a limited app',
  'ios': [18, 26],
  'text': 'When an App Limit runs out, opening the app shows a "Time Limit" screen: "You\'ve reached your limit on '
          '<app>". Buttons: "OK" closes it and returns Home; "Ask For More Time" (or "Ignore Limit" when no Screen '
          'Time passcode blocks it) leads to time-extension choices. With a passcode, you must enter it. During '
          'Downtime the apps are dimmed with an hourglass.',
  'keywords': ['time limit', 'ignore limit', 'ask for more time', 'app limit reached', 'hourglass', 'blocked app'],
  'screens': ['alert', 'com.apple.springboard'],
  'source': ['get-started-with-screen-time-iphbfa595995', 'set-schedules-with-screen-time-iphb0c7313c9']},
 {'id': 'panels.screen_time.settings',
  'topic': 'panels',
  'title': 'Screen Time settings',
  'ios': [18, 26],
  'text': 'Settings › Screen Time. Turn on "App & Website Activity" first; it also drives reports, "Downtime", "App '
          'Limits" and "Always Allowed". Downtime: tap "Downtime", then "Scheduled" or "Turn On Downtime Until '
          'Tomorrow". Limits: "App Limits" › "Add Limit", pick categories or apps, "Next", set time. To turn it all '
          'off, scroll down and tap "Turn Off App & Website Activity". A Screen Time passcode may be asked for.',
  'keywords': ['screen time', 'downtime', 'app limits', 'parental controls', 'always allowed', 'passcode'],
  'screens': ['com.apple.Preferences'],
  'source': ['get-started-with-screen-time-iphbfa595995', 'set-schedules-with-screen-time-iphb0c7313c9']},
 {'id': 'panels.sound.silent_volume',
  'topic': 'panels',
  'title': 'Silent mode and volume',
  'ios': [18, 26],
  'text': 'Silent mode stops ringing and alerts (vibration may continue). Models with a Ring/Silent switch: flip it '
          'toward the back for Silent. Models with an Action button: Settings › Sounds & Haptics, or set the Action '
          'button to Silent Mode. Volume buttons change media volume during calls or media, otherwise ringer volume; '
          'or open Control Center and drag the volume slider. Alarms still sound in Silent mode.',
  'keywords': ['mute', 'ring silent switch', 'sound off', 'volume up', 'vibrate', 'ringer'],
  'screens': ['com.apple.springboard', 'com.apple.Preferences'],
  'source': ['silence-iphone-iph81c7fd7d1', 'adjust-the-volume-iphb71f9b54d']},
 {'id': 'panels.standby',
  'topic': 'panels',
  'title': 'StandBy',
  'ios': [18, 26],
  'text': 'StandBy is a full-screen clock, photo or widget view that appears when iPhone is charging, on its side '
          'and stationary (also via MagSafe). Swipe left or right to change view, up or down for options. If the '
          'screen is off, tap it or press the side button. Turn it on or off at Settings › StandBy. An Always-On '
          'display keeps it lit; other models wake on a tap.',
  'keywords': ['bedside clock', 'landscape charging', 'night mode', 'always on display'],
  'screens': ['com.apple.springboard', 'com.apple.Preferences'],
  'source': ['use-standby-iph878d77632']},
 {'id': 'panels.wifi.ask_to_join',
  'topic': 'panels',
  'title': 'Ask to Join Networks and the join prompt',
  'ios': [18, 26],
  'text': 'Settings › Wi-Fi › "Ask to Join Networks" has "Off", "Notify" or "Ask". With "Ask", iOS shows a prompt '
          'listing nearby networks when no known one is found; tap one to join it. "Notify" only shows a '
          'notification. "Auto-Join Hotspot" ("Never", "Ask to Join", "Automatic") controls joining a Personal '
          'Hotspot. Tapping the info button next to a network shows its settings.',
  'keywords': ['wifi prompt', 'auto join', 'nearby networks', 'hotspot', 'forget network'],
  'screens': ['com.apple.Preferences', 'alert'],
  'source': ['connect-to-the-internet-iphd1cf4268']},
 {'id': 'panels.wifi.join',
  'topic': 'panels',
  'title': 'Join a Wi-Fi network',
  'ios': [18, 26],
  'text': 'Settings › Wi-Fi, switch Wi-Fi on, then tap a network name under "Networks". If a password sheet appears, '
          'type it and tap "Join". Hidden network: tap "Other", enter name, security and password. A blue checkmark '
          'appears by the connected network and the Wi-Fi icon shows in the status bar. A Personal Hotspot also '
          'shows in this list.',
  'keywords': ['connect wifi', 'wifi password', 'join network', 'hotspot', 'hidden network', 'other'],
  'screens': ['com.apple.Preferences', 'alert'],
  'source': ['connect-to-the-internet-iphd1cf4268']},
 {'id': 'settings.a11y.display_text.ios18',
  'topic': 'settings',
  'title': 'Accessibility › Display & Text Size',
  'ios': [18, 18],
  'text': 'Settings › Accessibility › Display & Text Size holds "Bold Text", "Larger Text", "Button Shapes", "On/Off '
          'Labels", "Reduce Transparency", "Increase Contrast", "Differentiate Without Color", "Smart Invert", '
          '"Classic Invert" and "Color Filters". All change what the agent sees. Remember what you changed and '
          'restore it after the task.',
  'keywords': ['accessibility', 'visual', 'contrast', 'invert', 'color filters', 'legibility'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-display-colors-iph3e2e1fb0', 'adjust-iphone-display-and-text-settings-iphd6804774e']},
 {'id': 'settings.a11y.display_text.ios26',
  'topic': 'settings',
  'title': 'Accessibility › Display & Text Size',
  'ios': [26, 26],
  'text': 'Settings › Accessibility › Display & Text Size holds "Bold Text", "Larger Text", "Show Borders" (Button '
          'Shapes before iOS 26), "On/Off Labels", "Reduce Transparency", "Increase Contrast", "Differentiate '
          'Without Color", "Prefer Horizontal Text", "Smart Invert", "Classic Invert" and "Color Filters". All '
          'change what the agent sees. Remember what you changed and restore it after the task.',
  'keywords': ['accessibility', 'visual', 'contrast', 'invert', 'color filters', 'legibility'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-display-colors-iph3e2e1fb0', 'adjust-iphone-display-and-text-settings-iphd6804774e']},
 {'id': 'settings.a11y.keyboards.ios18',
  'topic': 'settings',
  'title': 'Accessibility › Keyboards',
  'ios': [18, 18],
  'text': 'Settings › Accessibility › Keyboards (the row is just "Keyboards" on iOS 18) has "Show Lowercase Keys". '
          'Turn it off and the onscreen keys always show capitals, which changes the keyboard labels the agent '
          'reads. Key Repeat, Sticky Keys and Slow Keys apply to external keyboards.',
  'keywords': ['lowercase keys', 'uppercase keyboard', 'caps', 'keyboard labels', 'sticky keys'],
  'screens': ['com.apple.Preferences'],
  'source': ['adjust-keyboard-settings-ipha7c3927eb']},
 {'id': 'settings.a11y.keyboards.ios26',
  'topic': 'settings',
  'title': 'Accessibility › Keyboards & Typing',
  'ios': [26, 26],
  'text': 'Settings › Accessibility › Keyboards & Typing has "Show Lowercase Keys". Turn it off and the onscreen '
          'keys always show capitals, which changes the keyboard labels the agent reads. Key Repeat, Sticky Keys and '
          'Slow Keys apply to external keyboards.',
  'keywords': ['lowercase keys', 'uppercase keyboard', 'caps', 'keyboard labels', 'sticky keys'],
  'screens': ['com.apple.Preferences'],
  'source': ['adjust-keyboard-settings-ipha7c3927eb']},
 {'id': 'settings.a11y.motion.ios18',
  'topic': 'settings',
  'title': 'Accessibility › Motion',
  'ios': [18, 18],
  'text': 'Settings › Accessibility › Motion has "Reduce Motion", "Auto-Play Message Effects", "Auto-Play Video '
          'Previews", "Auto-Play Animated Images", "Dim Flashing Lights", "Prefer Non-Blinking Cursor" and "Limit '
          'Frame Rate". Reduce Motion shortens transitions so the next screen settles sooner; Prefer Non-Blinking '
          'Cursor stops the caret flicker.',
  'keywords': ['reduce motion', 'animations', 'transitions', 'blinking cursor', 'autoplay', 'gif'],
  'screens': ['com.apple.Preferences'],
  'source': ['customize-onscreen-motion-iph0b691d3ed']},
 {'id': 'settings.a11y.motion.ios26',
  'topic': 'settings',
  'title': 'Accessibility › Motion',
  'ios': [26, 26],
  'text': 'Settings › Accessibility › Motion has "Reduce Motion", "Vehicle Motion Cues", "Auto-Play Animated '
          'Images", "Auto-Play Video Previews", "Auto-Play Message Effects", "Prefer Non-Blinking Cursor" and "Limit '
          'Frame Rate". Reduce Motion shortens transitions so the next screen settles sooner. "Dim Flashing Lights" '
          'and "Reduce Bright Effects" are under Display & Text Size.',
  'keywords': ['reduce motion', 'animations', 'transitions', 'blinking cursor', 'autoplay', 'gif'],
  'screens': ['com.apple.Preferences'],
  'source': ['customize-onscreen-motion-iph0b691d3ed']},
 {'id': 'settings.a11y.per_app',
  'topic': 'settings',
  'title': 'Per-App Settings for accessibility',
  'ios': [18, 26],
  'text': 'Settings › Accessibility › "Per-App Settings" › "Add App" lets you pick one app, or "Home Screen & App '
          'Library", then tap its name to set text size, bold, contrast, transparency and motion for only that app. '
          'A UI that looks different in one app may be using this.',
  'keywords': ['per app', 'app specific', 'text size one app', 'visual settings'],
  'screens': ['com.apple.Preferences'],
  'source': ['customize-per-app-visual-settings-iph1f48544ab']},
 {'id': 'settings.a11y.touch',
  'topic': 'settings',
  'title': 'Accessibility › Touch: AssistiveTouch, Reachability, Back Tap',
  'ios': [18, 26],
  'text': 'Settings › Accessibility › Touch holds "AssistiveTouch", "Reachability" and "Back Tap". AssistiveTouch '
          'adds a floating menu button on every screen that can cover content; drag it aside. Reachability: swipe '
          'down on the bottom edge to lower the top half of the screen, tap the upper half to restore. Back Tap runs '
          'an action when the back is tapped twice or three times.',
  'keywords': ['assistivetouch', 'floating button', 'reachability', 'back tap', 'one hand', 'touch accommodations'],
  'screens': ['com.apple.Preferences'],
  'source': ['use-assistivetouch-iph96b21954', 'use-reachability-iph145eba8e9', 'back-tap-iphaa57e7885']},
 {'id': 'settings.a11y.ui_effects.ios18',
  'topic': 'settings',
  'title': 'What each display accessibility setting changes on screen',
  'ios': [18, 18],
  'text': '"Larger Text" makes rows taller and truncates labels. "Bold Text" thickens glyphs. "Button Shapes" '
          'underlines or outlines text buttons. "On/Off Labels" draws a line (on) or circle (off) on switches. '
          '"Reduce Transparency" makes blurred bars solid. "Smart Invert" flips colors except images. "Differentiate '
          'Without Color" adds shapes to colored status cues.',
  'keywords': ['larger text',
               'button shapes',
               'show borders',
               'i/o switch',
               'invert colors',
               'layout change',
               'truncated'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-display-colors-iph3e2e1fb0', 'adjust-iphone-display-and-text-settings-iphd6804774e']},
 {'id': 'settings.a11y.ui_effects.ios26',
  'topic': 'settings',
  'title': 'What each display accessibility setting changes on screen',
  'ios': [26, 26],
  'text': '"Larger Text" makes rows taller and truncates labels. "Bold Text" thickens glyphs. "Show Borders" '
          'underlines or outlines text buttons. "On/Off Labels" draws a line (on) or circle (off) on switches. '
          '"Reduce Transparency" makes blurred bars solid. "Smart Invert" flips colors except images. "Differentiate '
          'Without Color" adds shapes to colored status cues.',
  'keywords': ['larger text',
               'button shapes',
               'show borders',
               'i/o switch',
               'invert colors',
               'layout change',
               'truncated'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-display-colors-iph3e2e1fb0', 'adjust-iphone-display-and-text-settings-iphd6804774e']},
 {'id': 'settings.about',
  'topic': 'settings',
  'title': 'Check iOS version, model and capacity',
  'ios': [18, 26],
  'text': 'Settings › General › About lists "Name", "iOS Version", "Model Name", "Model Number", "Serial Number", '
          '"Capacity", "Available" and carrier rows. Read "iOS Version" there. Serial number, IMEI and addresses are '
          'identifiers: touch and hold one to copy; don\'t paste them anywhere. Search "About" to jump straight to '
          'it.',
  'keywords': ['ios version', 'which ios', 'model', 'serial', 'imei', 'capacity', 'device info'],
  'screens': ['com.apple.Preferences'],
  'source': ['get-information-about-your-iphone-iph3dd5fc7e']},
 {'id': 'settings.apps',
  'topic': 'settings',
  'title': 'Per-app settings live under Apps',
  'ios': [18, 26],
  'text': 'Since iOS 18, per-app settings (Mail, Safari, Messages, Photos, Camera-related app options, App Store and '
          'others) are not rows on the root list. Scroll to the bottom, tap "Apps", then tap the app. On iOS 17 and '
          "earlier they sat in a long list below the main groups. Searching the app's name in Settings search also "
          'finds them.',
  'keywords': ['app settings',
               'safari settings',
               'mail settings',
               'messages settings',
               'photos settings',
               'app list'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-settings-iph079e1fe9d', 'manage-storage-on-iphone-iph47c931112']},
 {'id': 'settings.back',
  'topic': 'settings',
  'title': 'Go back and up in Settings',
  'ios': [18, 26],
  'text': 'Each Settings page has a Back button at the top left named after its parent page ("Settings", "General"). '
          'Tap it to go up one level. To jump anywhere else, go back to the root list or use Settings search. A '
          'switch row toggles when tapped; a row with a chevron opens a page.',
  'keywords': ['back button', 'navigate', 'chevron', 'parent page', 'up one level'],
  'screens': ['com.apple.Preferences'],
  'source': ['checked on iOS 26.0.1 (Mobster Settings pack)']},
 {'id': 'settings.battery.ios18',
  'topic': 'settings',
  'title': 'Battery page',
  'ios': [18, 18],
  'text': 'Settings › Battery shows level and usage. Turn on "Battery Percentage" to show it in the status bar. "Low '
          'Power Mode" is a switch on this page. Tap "Battery Health & Charging" for capacity, peak performance and '
          'service state. Low Power Mode switches itself off at 80 percent charge.',
  'keywords': ['battery percentage', 'low power mode', 'battery health', 'charge', 'power saving'],
  'screens': ['com.apple.Preferences'],
  'source': ['show-the-battery-percentage-iph4747a74a9',
             'save-battery-life-with-power-modes-on-iphone-iphcab9aecd1',
             'understand-iphone-battery-usage-health--iphd453d043a']},
 {'id': 'settings.battery.ios26',
  'topic': 'settings',
  'title': 'Battery page',
  'ios': [26, 26],
  'text': 'Settings › Battery shows level and usage. Turn on "Battery Percentage" to show it in the status bar. '
          '"Power Mode" (iPhone 15 and later) holds "Low Power Mode" and "Adaptive Power"; on iPhone 14 and earlier '
          '"Low Power Mode" is directly on Battery. "Battery Health" (iPhone 14 and earlier: "Battery Health & '
          'Charging") shows capacity and service state.',
  'keywords': ['battery percentage', 'low power mode', 'battery health', 'charge', 'power saving', 'adaptive power'],
  'screens': ['com.apple.Preferences'],
  'source': ['show-the-battery-percentage-iph4747a74a9',
             'save-battery-life-with-power-modes-on-iphone-iphcab9aecd1',
             'understand-iphone-battery-usage-health--iphd453d043a',
             'checked on iOS 26.0.1 (Mobster Settings pack)']},
 {'id': 'settings.date_time',
  'topic': 'settings',
  'title': 'Date & Time and time zone',
  'ios': [18, 26],
  'text': 'Settings › General › Date & Time has "24-Hour Time" and "Set Automatically". To set a time zone by hand, '
          'turn off "Set Automatically", tap "Time Zone", type a city in the search field and tap the result. With '
          'Set Automatically on, Location Services sets the zone.',
  'keywords': ['time zone', '24-hour', 'clock', 'wrong time', 'set automatically'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-the-date-and-time-iph65f82af3e']},
 {'id': 'settings.deep_links',
  'topic': 'settings',
  'title': 'Settings deep links (unreliable)',
  'ios': [18, 26],
  'text': 'Only the documented UIApplication.openSettingsURLString / "app-settings:" is supported: it opens the '
          'calling app\'s own Settings page. "prefs:" and "App-prefs:" are undocumented and unsupported by Apple; '
          'sub-pages broke in iOS 18 (Apple forum threads 691712, 759900). Only root "App-prefs:" reliably opens '
          'Settings; anything else is best effort. Use Settings search instead.',
  'keywords': ['open settings url', 'app-prefs', 'prefs:', 'openSettingsURLString', 'app-settings', 'url scheme'],
  'screens': ['any'],
  'source': ['developer forums threads 691712 and 759900', 'not verified on iOS 26 (Mobster Settings pack)']},
 {'id': 'settings.display_brightness.ios18',
  'topic': 'settings',
  'title': 'Display & Brightness',
  'ios': [18, 18],
  'text': 'Settings › Display & Brightness holds brightness, Light/Dark appearance, "True Tone", "Night Shift", '
          '"Auto-Lock", "Raise to Wake", "Always On Display", "Text Size", "Bold Text" and "Display Zoom". Auto-Lock '
          '› pick "Never" to keep the screen on during a long task. Light/Dark controls sit at the top.',
  'keywords': ['dark mode', 'light mode', 'brightness', 'auto-lock', 'screen timeout', 'true tone', 'text size'],
  'screens': ['com.apple.Preferences'],
  'source': ['adjust-iphone-display-and-text-settings-iphd6804774e',
             'keep-the-iphone-display-on-longer-iph7117338a8']},
 {'id': 'settings.display_brightness.ios26',
  'topic': 'settings',
  'title': 'Display & Brightness',
  'ios': [26, 26],
  'text': 'Settings › Display & Brightness holds brightness, Light/Dark appearance, "True Tone", "Night Shift", '
          '"Auto-Lock", "Raise to Wake", "Always On Display", "Text Size", "Bold Text", "Display Zoom" and "Liquid '
          'Glass" (Clear or Tinted). Auto-Lock › pick "Never" to keep the screen on during a long task. Light/Dark '
          'controls sit at the top.',
  'keywords': ['dark mode',
               'light mode',
               'brightness',
               'auto-lock',
               'screen timeout',
               'true tone',
               'text size',
               'liquid glass'],
  'screens': ['com.apple.Preferences'],
  'source': ['adjust-iphone-display-and-text-settings-iphd6804774e',
             'keep-the-iphone-display-on-longer-iph7117338a8']},
 {'id': 'settings.face_id_passcode',
  'topic': 'settings',
  'title': 'Face ID & Passcode',
  'ios': [18, 26],
  'text': 'Settings › Face ID & Passcode (Touch ID & Passcode on Home button models) asks for the passcode before '
          'opening. Never enter or change a passcode for the user: stop and hand control back. The page holds "Turn '
          'Passcode Off", "Change Passcode", "Erase Data" and what is allowed when locked.',
  'keywords': ['passcode', 'unlock', 'face id', 'lock screen access', 'erase data', 'security'],
  'screens': ['com.apple.Preferences'],
  'source': ['set-a-passcode-iph14a867ae']},
 {'id': 'settings.home_screen_app_library',
  'topic': 'settings',
  'title': 'Where new apps go',
  'ios': [18, 26],
  'text': 'Settings › Home Screen & App Library has "Add to Home Screen" and "App Library Only" under Newly '
          'Downloaded Apps. With App Library Only, a freshly installed app has no Home Screen icon: find it by '
          'swiping left past the last page and searching the App Library. "Show in App Library" turns on badges '
          'there.',
  'keywords': ['newly downloaded apps', 'new app missing', 'install', 'app library only', 'badges'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-and-use-your-apps-in-app-library-iph87abad19a']},
 {'id': 'settings.iphone_storage',
  'topic': 'settings',
  'title': 'See free space and storage use per app',
  'ios': [18, 26],
  'text': 'Settings › General › iPhone Storage shows used and total space ("125 GB of 128 GB used") and apps sorted '
          'by size. Tap an app for "Offload App" or "Delete App": both change the phone, so ask first. On iOS 18 '
          '"Offload Unused Apps" is here; on iOS 26 it is under Settings › Apps › App Store.',
  'keywords': ['free space', 'storage full', 'how much space', 'offload', 'apps by size', 'gb'],
  'screens': ['com.apple.Preferences'],
  'source': ['manage-storage-on-iphone-iph47c931112']},
 {'id': 'settings.keyboard',
  'topic': 'settings',
  'title': 'Keyboard settings: auto-correction, predictive, spelling',
  'ios': [18, 26],
  'text': 'Settings › General › Keyboard has switches for "Auto-Correction", "Check Spelling", "Predictive Text", '
          '"Smart Punctuation", "Auto-Capitalization" and "Enable Dictation", plus "Keyboards". While typing, touch '
          'and hold the globe or emoji key, then tap "Keyboard Settings" to get there. Turning off Predictive Text '
          'also removes inline suggestions.',
  'keywords': ['autocorrect', 'autocapitalization', 'smart punctuation', 'dictation', 'typing', 'keyboard switches'],
  'screens': ['com.apple.Preferences'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e', 'use-predictive-text-iphd4ea90231']},
 {'id': 'settings.keyboard_feedback',
  'topic': 'settings',
  'title': 'Keyboard click sound and haptics',
  'ios': [18, 26],
  'text': 'Settings › Sounds & Haptics › Keyboard Feedback has "Sound" and "Haptic" switches for key clicks and taps '
          'while typing.',
  'keywords': ['keyboard click', 'typing sound', 'key haptic', 'vibration'],
  'screens': ['com.apple.Preferences'],
  'source': ['type-with-the-onscreen-keyboard-iph3c50f96e']},
 {'id': 'settings.language_region.ios18',
  'topic': 'settings',
  'title': 'Language & Region',
  'ios': [18, 18],
  'text': 'Settings › General › Language & Region sets the iPhone language, preferred form of address, region, '
          'calendar, temperature unit, measurement system, first day of week and Live Text. "Add Language" adds a '
          'language and keyboard. Changing the language changes every label the agent reads.',
  'keywords': ['language', 'region', 'celsius', 'fahrenheit', 'units', 'measurement', 'locale'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-the-language-and-region-iphce20717a3']},
 {'id': 'settings.language_region.ios26',
  'topic': 'settings',
  'title': 'Language & Region',
  'ios': [26, 26],
  'text': 'Settings › General › Language & Region sets the iPhone language, region, calendar, temperature unit, '
          'measurement system, first day of week, date format, number format and Live Text. "Add Language" adds a '
          'language and keyboard. Changing the language restarts the interface and can change every label the agent '
          'reads.',
  'keywords': ['language', 'region', 'celsius', 'fahrenheit', 'units', 'measurement', 'locale'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-the-language-and-region-iphce20717a3']},
 {'id': 'settings.layout.ios18',
  'topic': 'settings',
  'title': 'Settings root list layout',
  'ios': [18, 18],
  'text': 'Scroll the root list: Apple Account row, Airplane Mode, Wi-Fi, Bluetooth, Cellular, Personal Hotspot, '
          'Battery; General, Accessibility, Action Button, Camera, Control Center, Display & Brightness, Home Screen '
          '& App Library, Search, Siri, StandBy, Wallpaper; Notifications, Sounds & Haptics, Focus, Screen Time; '
          'Face ID & Passcode, Emergency SOS, Privacy & Security; Game Center, iCloud, Wallet & Apple Pay; then '
          '"Apps" at the bottom. Rows vary by model.',
  'keywords': ['settings list', 'top level', 'root', 'sections', 'order', 'scroll'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-settings-iph079e1fe9d']},
 {'id': 'settings.layout.ios26',
  'topic': 'settings',
  'title': 'Settings root list layout',
  'ios': [26, 26],
  'text': 'Scroll the root list from the top: Apple Account row, Airplane Mode, Wi-Fi, Bluetooth, Cellular, Personal '
          'Hotspot, Battery; then General, Accessibility, Action Button, Camera, Control Center, Display & '
          'Brightness, Home Screen & App Library, Search, Siri ("Apple Intelligence & Siri" on Apple Intelligence '
          'models), StandBy, Wallpaper; Notifications, Sounds & Haptics, Focus, Screen Time; Face ID & Passcode, '
          'Emergency SOS, Privacy & Security; Game Center, iCloud, Wallet & Apple Pay, Apps. Rows vary by model.',
  'keywords': ['settings list', 'top level', 'root', 'sections', 'order', 'scroll'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-settings-iph079e1fe9d', 'change-the-settings-iphc778c5c07']},
 {'id': 'settings.location_services',
  'topic': 'settings',
  'title': 'Location Services',
  'ios': [18, 26],
  'text': 'Settings › Privacy & Security › Location Services has the master switch "Location Services" and one row '
          'per app. Tap an app to pick Never, Ask Next Time, While Using the App or Always. "System Services" at the '
          'bottom controls built-in features. Turning the master switch off breaks many features, including '
          'automatic time zone.',
  'keywords': ['location permission', 'gps', 'while using', 'always allow', 'precise location'],
  'screens': ['com.apple.Preferences'],
  'source': ['control-the-location-information-you-share-iph3dd5f9be']},
 {'id': 'settings.notifications',
  'topic': 'settings',
  'title': 'Notification settings per app',
  'ios': [18, 26],
  'text': 'Settings › Notifications lists every app under "Notification Style". Tap an app to find "Allow '
          'Notifications", "Sounds", Lock Screen, Banner and "Show Previews" options. The top of the page picks '
          'Count, Stack or List for the Lock Screen and holds "Scheduled Summary". Turning off Allow Notifications '
          'stops banners for that app.',
  'keywords': ['turn off notifications', 'banners', 'badges', 'sounds', 'previews', 'scheduled summary', 'alerts'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-notification-settings-iph7c3d96bab']},
 {'id': 'settings.privacy_security',
  'topic': 'settings',
  'title': 'Privacy & Security permissions',
  'ios': [18, 26],
  'text': 'Settings › Privacy & Security lists "Location Services", "Tracking", and one row per data type (Photos, '
          'Camera, Microphone, Contacts and others). Tap a row to see which apps have access, then tap an app to '
          'change it. Setting an app\'s Location access to "Ask Next Time" re-prompts later. "Analytics & '
          'Improvements" is here too.',
  'keywords': ['permissions',
               'app access',
               'camera permission',
               'photos permission',
               'location',
               'tracking',
               'allow access'],
  'screens': ['com.apple.Preferences'],
  'source': ['control-the-location-information-you-share-iph3dd5f9be',
             'control-app-tracking-permissions-iph4f4cbd242']},
 {'id': 'settings.screen_time',
  'topic': 'settings',
  'title': 'Screen Time',
  'ios': [18, 26],
  'text': 'Settings › Screen Time shows the daily summary; "See All App & Website Activity" gives Day and Week '
          'views. Tap "App & Website Activity" to turn tracking on. App limits, Downtime and Content & Privacy '
          'Restrictions are here and can lock apps, so a Screen Time passcode may block changes. "Share Across '
          'Devices" is near the bottom.',
  'keywords': ['app limits', 'downtime', 'usage', 'restrictions', 'parental controls', 'screen time passcode'],
  'screens': ['com.apple.Preferences'],
  'source': ['get-started-with-screen-time-iphbfa595995']},
 {'id': 'settings.search.ios18',
  'topic': 'settings',
  'title': 'Find any setting with Settings search',
  'ios': [18, 18],
  'text': 'Fastest route to any setting. Open Settings, swipe down on the root list to reveal the "Search" field at '
          "the top, tap it, type the setting's name, then tap the result. Each result shows the setting and its "
          'parent, like "Battery Health, Battery". If the field is hidden, you are scrolled down or on a sub-page: '
          'go back to the root list first.',
  'keywords': ['search settings', 'find setting', 'pull down', 'go to setting', 'shortcut'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-settings-iph079e1fe9d']},
 {'id': 'settings.search.ios26',
  'topic': 'settings',
  'title': 'Find any setting with Settings search',
  'ios': [26, 26],
  'text': 'Fastest route to any setting. Open Settings, tap the "Search" field at the bottom of the screen (it stays '
          "there on every page), type the setting's name, then tap the result. Each result shows the setting and its "
          'parent, like "Battery Health, Battery". Tapping opens that page directly. Clear with "Clear text" or '
          '"close".',
  'keywords': ['search settings', 'find setting', 'go to setting', 'search field', 'shortcut'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-settings-iph079e1fe9d', 'checked on iOS 26.0.1 and 26.4 (Mobster Settings pack)']},
 {'id': 'settings.search.no_results',
  'topic': 'settings',
  'title': 'Settings search finds nothing',
  'ios': [18, 26],
  'text': 'If Settings\' search answers "No Results for “…”" for a setting that exists (a simulator\'s or a freshly '
          "set-up phone's search index can be empty), close the search and go by the page's path instead, for "
          'example Settings › Accessibility › Motion › "Reduce Motion". Scroll a page to find a row lower down.',
  'keywords': ['no results', 'settings search', "can't find setting", 'path', 'navigate settings'],
  'screens': ['com.apple.Preferences'],
  'source': ['recorded: iOS 26.4 simulator Settings search, 14 Oct 2026']},
 {'id': 'settings.search_spotlight',
  'topic': 'settings',
  'title': 'Settings › Search and Show App in Search',
  'ios': [18, 26],
  'text': 'Settings › Search lists Spotlight suggestion switches and every installed app. Tap an app, then toggle '
          '"Show App in Search" to include or hide it in Spotlight. A hidden app still opens from the Home Screen or '
          'App Library.',
  'keywords': ['spotlight', 'hide app from search', 'siri suggestions', 'show content in search'],
  'screens': ['com.apple.Preferences'],
  'source': ['find-settings-iph079e1fe9d']},
 {'id': 'settings.software_update',
  'topic': 'settings',
  'title': 'Check for or install an iOS update',
  'ios': [18, 26],
  'text': 'Settings › General › Software Update shows the installed iOS version and whether an update is available. '
          '"Download and Install" starts a real update that restarts the phone: treat it as consequential. '
          '"Automatic Updates" holds the "Automatically Install" and "Automatically Download" switches. On iOS 26 it '
          'also has a System Files option.',
  'keywords': ['update ios', 'upgrade', 'new version', 'automatic updates', 'download and install'],
  'screens': ['com.apple.Preferences'],
  'source': ['update-ios-iph3e504502']},
 {'id': 'settings.sounds_haptics',
  'topic': 'settings',
  'title': 'Sounds & Haptics',
  'ios': [18, 26],
  'text': 'Settings › Sounds & Haptics holds the ringer and alerts volume slider, "Ringtone", "Text Tone", vibration '
          'options, "Keyboard Feedback", "Lock Sound" and system haptics. The silent switch or Silent Mode in '
          'Control Center is separate from these.',
  'keywords': ['volume', 'ringtone', 'vibrate', 'silent', 'text tone', 'haptic', 'lock sound'],
  'screens': ['com.apple.Preferences'],
  'source': ['change-sounds-and-vibrations-iph07c867f28']},
 {'id': 'settings.text_size',
  'topic': 'settings',
  'title': 'Change text size and bold text',
  'ios': [18, 26],
  'text': 'Settings › Display & Brightness › "Text Size", then drag the slider. "Bold Text" is a switch on the same '
          'page. "Display Zoom" › "Larger Text" › "Use Zoomed" magnifies the whole interface (some models). Bigger '
          'text makes rows taller, so lists need more scrolling and long labels truncate.',
  'keywords': ['font size', 'larger text', 'bigger text', 'zoomed', 'readability'],
  'screens': ['com.apple.Preferences'],
  'source': ['adjust-iphone-display-and-text-settings-iphd6804774e']},
 {'id': 'settings.tracking',
  'topic': 'settings',
  'title': 'App tracking permission',
  'ios': [18, 26],
  'text': 'Settings › Privacy & Security › Tracking has "Allow Apps to Request to Track" and a list of apps that '
          'asked. Turn an app\'s switch off to deny tracking. This is separate from the "Allow Tracking" and "Ask '
          'App Not to Track" pop-ups an app shows on first launch.',
  'keywords': ['ask app not to track', 'ad tracking', 'att', 'privacy prompt'],
  'screens': ['com.apple.Preferences'],
  'source': ['control-app-tracking-permissions-iph4f4cbd242']},
 {'id': 'settings.transfer_reset',
  'topic': 'settings',
  'title': 'Transfer or Reset iPhone (consequential)',
  'ios': [18, 26],
  'text': 'Settings › General › Transfer or Reset iPhone › "Reset" offers "Reset All Settings", "Reset Network '
          'Settings", "Reset Keyboard Dictionary", "Reset Home Screen Layout" and "Reset Location & Privacy" (iOS 26 '
          'adds "Delete All eSIMs", "Reset Handwriting Style"). "Erase All Content and Settings" wipes the phone. '
          'Never tap these without explicit approval.',
  'keywords': ['erase iphone', 'factory reset', 'reset network', 'wipe', 'reset settings'],
  'screens': ['com.apple.Preferences'],
  'source': ['reset-iphone-settings-iphea1c2fe48']},
 {'id': 'settings.wifi_bluetooth_cellular',
  'topic': 'settings',
  'title': 'Wi-Fi, Bluetooth, Cellular, Personal Hotspot',
  'ios': [18, 26],
  'text': 'These four rows are near the top of the root list. "Wi-Fi" lists networks (tap one to join). "Bluetooth" '
          'has a switch and the device list; tap the info button by a device to rename or forget it. "Cellular" '
          'holds mobile data, roaming and per-app data. "Personal Hotspot" shares the connection. Forgetting or '
          'joining networks changes the phone: confirm first.',
  'keywords': ['wifi', 'join network', 'pair', 'mobile data', 'hotspot', 'roaming', 'internet'],
  'screens': ['com.apple.Preferences'],
  'source': ['connect-bluetooth-accessories-iph3c50f191',
             'share-your-internet-connection-iph45447ca6',
             'view-or-change-cellular-data-settings-iph3dd5f213']},
 {'id': 'share.sheet.close',
  'topic': 'share',
  'title': 'Close the share sheet',
  'ios': [18, 26],
  'text': 'Tap the "x" at the top right where shown, or swipe down on the sheet, or tap the dimmed area above it. If '
          'the sheet only dismisses to a compose view, tap "Cancel" there. Closing sends nothing.',
  'keywords': ['dismiss share', 'cancel share', 'close sheet'],
  'screens': ['any'],
  'source': ['share-photos-links-and-more-iphb66cfeaad']},
 {'id': 'share.sheet.consequential',
  'topic': 'share',
  'title': 'Share targets send',
  'ios': [18, 26],
  'text': 'Tapping a person, an AirDrop device or an app in the share sheet starts a send or opens a compose view; '
          'that is consequential. "Copy", "Save to Files" and "Add to Reading List" stay on the phone. A recipient '
          'can accept or decline an AirDrop. Ask before choosing a person or app.',
  'keywords': ['airdrop send', 'messages send', 'who to send'],
  'screens': ['any'],
  'source': ['use-airdrop-to-send-items-to-nearby-devices-iphcd8b9f0af']},
 {'id': 'share.sheet.layout',
  'topic': 'share',
  'title': 'Share sheet rows',
  'ios': [18, 26],
  'text': 'Top: the item name and "Options". Then a row of people and AirDrop suggestions, a row of apps (Messages, '
          'Mail, Notes, ...), and a list of actions such as "Copy", "Add to Reading List", "Save to Files", "Print" '
          'and "Edit Actions..." at the bottom. Scroll to find a row.',
  'keywords': ['airdrop', 'actions list', 'copy link', 'reading list', 'save to files'],
  'screens': ['any'],
  'source': ['use-airdrop-to-send-items-to-nearby-devices-iphcd8b9f0af']},
 {'id': 'share.sheet.open',
  'topic': 'share',
  'title': 'Open the share sheet',
  'ios': [18, 26],
  'text': "Tap the Share button, a square with an up arrow, in Safari's toolbar, Photos, Files, Notes and most apps. "
          'Some apps show "Share" in a menu or on touch and hold. The sheet slides up over the app. Opening it sends '
          'nothing.',
  'keywords': ['share button', 'send', 'export', 'up arrow'],
  'screens': ['any'],
  'source': ['share-photos-links-and-more-iphb66cfeaad']},
 {'id': 'text.edit_menu.ios26',
  'topic': 'text',
  'title': 'Edit menu in iOS 26',
  'ios': [26, 26],
  'text': 'The guide lists the same edit commands as iOS 18: "Cut", "Copy", "Paste", "Select All", "Replace...", '
          '"Format", plus a more arrow. The menu can look different on screen, for instance a longer vertical list, '
          'so match items by label rather than position. Look Up, Translate and Share sit behind the more option.',
  'keywords': ['context menu', 'vertical menu', 'liquid glass menu', 'copy paste menu'],
  'screens': ['keyboard', 'any'],
  'source': ['select-and-edit-text-iph1a9cae52c']},
 {'id': 'text.edit_menu.items',
  'topic': 'text',
  'title': 'Edit menu items',
  'ios': [18, 18],
  'text': 'Tapping a selection shows "Cut", "Copy", "Paste", "Select All", "Replace..." and "Format"; the arrow or '
          'ellipsis opens more (Look Up, Translate, Share). Items depend on the field and selection: "Paste" shows '
          'only with clipboard content. Menu labels are tappable text.',
  'keywords': ['copy', 'cut', 'look up', 'translate', 'context menu'],
  'screens': ['keyboard', 'any'],
  'source': ['select-and-edit-text-iph1a9cae52c']},
 {'id': 'text.select.gestures',
  'topic': 'text',
  'title': 'Select text by gesture',
  'ios': [18, 26],
  'text': 'Double-tap a word to select it, triple-tap for a paragraph. For a block, double-tap and hold the first '
          'word, then drag the grab points. Tap a spot to place the cursor; touch and hold to magnify it and drag. '
          'Selected text can be dragged elsewhere by touching and holding until it lifts.',
  'keywords': ['highlight text', 'select word', 'select paragraph', 'handles', 'grab points'],
  'screens': ['keyboard', 'any'],
  'source': ['select-and-edit-text-iph1a9cae52c']},
 {'id': 'text.three_finger_gestures',
  'topic': 'text',
  'title': 'Three-finger edit gestures',
  'ios': [18, 26],
  'text': 'Pinch closed with three fingers to copy, twice to cut, and pinch open to paste. Swipe left with three '
          'fingers to undo and right to redo; a bar with "Undo" or "Redo" appears at the top. These need no menu but '
          'need a multi-touch swipe; fall back to the edit menu if they fail.',
  'keywords': ['pinch copy', 'pinch paste', 'gesture undo'],
  'screens': ['keyboard', 'any'],
  'source': ['select-and-edit-text-iph1a9cae52c', 'type-with-the-onscreen-keyboard-iph3c50f96e']}]
