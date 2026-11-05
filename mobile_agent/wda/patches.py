"""The source edits Mobster makes to WebDriverAgent; a source they don't fit fails the build rather than run open."""

import hashlib
import json
import plistlib
import re
import subprocess
from pathlib import Path

from .. import phone_ready
from ..devices import LOOPBACK
from . import gate as wda_gate
from . import source as wda_source

# Rebranded so free/personal teams can sign them (the stock com.facebook ids fail). The runner's id
# also gets the team's suffix at build time (signing.runner_bundle_id): App IDs are unique across
# every Apple team, so a free account can't register an id someone else's team already has.
BUNDLE_REBRAND = {"com.facebook.WebDriverAgentRunner": "app.mobster.wda.runner",
                  "com.facebook.WebDriverAgentLib": "app.mobster.wda.lib"}
# The runner earlier versions installed under the plain id; removed after the first build with the suffix.
LEGACY_RUNNER = "app.mobster.wda.runner.xctrunner"
RUNNER_ID = re.compile(
    r"(?<![\w.])(?:com\.facebook\.WebDriverAgentRunner|app\.mobster\.wda\.runner(?:\.[a-z0-9]{10})?)(?=[\s\";])")

WDA_LOCAL_HOST = """
// Mobster: loopback clients only (the USB and Wi-Fi relays). Mobster's own clients always send Host, so a request
// without one is refused too.
static BOOL FBMobsterLocalHost(NSString * _Nullable host)
{
  if (0 == host.length) {
    return NO;
  }
  NSString *name = (NSString * _Nonnull)host.lowercaseString;
  BOOL bracketed = [name hasPrefix:@"["];
  NSRange end = bracketed ? [name rangeOfString:@"]"] : [name rangeOfString:@":"];
  if (NSNotFound != end.location) {
    name = [name substringToIndex:end.location + (bracketed ? 1 : 0)];
  }
  return [name isEqualToString:@"127.0.0.1"] || [name isEqualToString:@"localhost"] || [name isEqualToString:@"[::1]"];
}
"""
WDA_LOCAL_STREAM = """
static BOOL FBMobsterLocalStreamRequest(NSData *data)
{
  NSString *head = [[NSString alloc] initWithData:data encoding:NSISOLatin1StringEncoding].lowercaseString;
  NSString *host = nil;
  for (NSString *line in [head componentsSeparatedByString:@"\\r\\n"]) {
    if ([line hasPrefix:@"origin:"]) {
      return NO;
    }
    if ([line hasPrefix:@"host:"]) {
      host = [[line substringFromIndex:5] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    }
  }
  return FBMobsterLocalHost(host);
}

"""
# Mobster's Photos routes (phone_io/photos.py, send_media): save a photo or video the Mac sends into the iPhone's Photos
# library. The runner's Info.plist (Xcode's XCTRunner template) carries NSPhotoLibraryUsageDescription, not the
# add-only key, so the read-write level is asked for; "Limit Access" is enough to add (Photos puts what an app adds in
# its selection). The first save asks on the iPhone; GET /mobster/photos/status says where that stands.
WDA_PHOTOS_ROUTES = """\
    // Mobster: save media into Photos (send_media).
    [[FBRoute GET:@"/mobster/photos/status"] respondWithTarget:self action:@selector(handleMobsterPhotosStatus:)],
    [[FBRoute GET:@"/mobster/photos/status"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosStatus:)],
    [[FBRoute POST:@"/mobster/photos"] respondWithTarget:self action:@selector(handleMobsterPhotosAdd:)],
    [[FBRoute POST:@"/mobster/photos"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosAdd:)],
"""  # noqa: E501
WDA_PHOTOS_COMMANDS = """
// Mobster: Photos (send_media). One photo or video, sent as base64, saved into the library as a new item.
static NSString *FBMobsterPhotosStatus(PHAuthorizationStatus status)
{
  switch (status) {
    case PHAuthorizationStatusAuthorized: return @"authorized";
    case PHAuthorizationStatusLimited: return @"limited";
    case PHAuthorizationStatusDenied: return @"denied";
    case PHAuthorizationStatusRestricted: return @"restricted";
    case PHAuthorizationStatusNotDetermined: return @"not_determined";
  }
  return @"not_determined";
}

+ (id<FBResponsePayload>)handleMobsterPhotosStatus:(FBRouteRequest *)request
{
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status),
                                @"bundleId": NSBundle.mainBundle.bundleIdentifier ?: @""});
}

+ (id<FBResponsePayload>)handleMobsterPhotosAdd:(FBRouteRequest *)request
{
  id encoded = request.arguments[@"data"];
  id kind = request.arguments[@"kind"];
  id extension = request.arguments[@"extension"];
  id filename = request.arguments[@"filename"];
  NSNumber *wait = [request.arguments[@"authorizationTimeout"] isKindOfClass:NSNumber.class]
    ? request.arguments[@"authorizationTimeout"] : @60;
  NSRegularExpression *plain = [NSRegularExpression regularExpressionWithPattern:@"^[A-Za-z0-9]{1,5}$" options:(NSRegularExpressionOptions)0 error:nil];
  if (![encoded isKindOfClass:NSString.class] || 0 == [(NSString *)encoded length]
      || ![extension isKindOfClass:NSString.class]
      || nil == [plain firstMatchInString:extension options:(NSMatchingOptions)0 range:NSMakeRange(0, [(NSString *)extension length])]
      || !([kind isEqual:@"photo"] || [kind isEqual:@"video"])) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"data, kind (photo or video) and extension are required" traceback:nil]);
  }
  NSData *data = [[NSData alloc] initWithBase64EncodedString:encoded options:NSDataBase64DecodingIgnoreUnknownCharacters];
  if (nil == data || 0 == data.length) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"data is not base64" traceback:nil]);
  }
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  if (PHAuthorizationStatusNotDetermined == status) {
    __block PHAuthorizationStatus answered = PHAuthorizationStatusNotDetermined;
    __block BOOL asked = NO;
    [PHPhotoLibrary requestAuthorizationForAccessLevel:PHAccessLevelReadWrite handler:^(PHAuthorizationStatus value) {
      answered = value;
      asked = YES;
    }];
    [[[[FBRunLoopSpinner new] timeout:MAX(1.0, MIN(wait.doubleValue, 120.0))] interval:0.2] spinUntilTrue:^BOOL{
      return asked;
    }];
    status = asked ? answered : PHAuthorizationStatusNotDetermined;
  }
  if (PHAuthorizationStatusAuthorized != status && PHAuthorizationStatusLimited != status) {
    return FBResponseWithObject(@{@"saved": @NO, @"status": FBMobsterPhotosStatus(status)});
  }
  NSString *name = [NSString stringWithFormat:@"mobster-%@.%@", NSUUID.UUID.UUIDString, extension];
  NSURL *file = [NSURL fileURLWithPath:[NSTemporaryDirectory() stringByAppendingPathComponent:name]];
  NSError *error;
  if (![data writeToURL:file options:NSDataWritingAtomic error:&error]) {
    return FBResponseWithUnknownError(error);
  }
  __block NSString *identifier = nil;
  __block BOOL finished = NO;
  __block BOOL saved = NO;
  __block NSError *failure = nil;
  [[PHPhotoLibrary sharedPhotoLibrary] performChanges:^{
    PHAssetCreationRequest *creation = [PHAssetCreationRequest creationRequestForAsset];
    PHAssetResourceCreationOptions *options = [PHAssetResourceCreationOptions new];
    options.shouldMoveFile = YES;
    if ([filename isKindOfClass:NSString.class] && [(NSString *)filename length] > 0) {
      options.originalFilename = filename;
    }
    [creation addResourceWithType:([kind isEqual:@"video"] ? PHAssetResourceTypeVideo : PHAssetResourceTypePhoto)
                          fileURL:file
                          options:options];
    identifier = creation.placeholderForCreatedAsset.localIdentifier;
  } completionHandler:^(BOOL success, NSError *changeError) {
    saved = success;
    failure = changeError;
    finished = YES;
  }];
  [[[[FBRunLoopSpinner new] timeout:120] interval:0.1] spinUntilTrue:^BOOL{
    return finished;
  }];
  [NSFileManager.defaultManager removeItemAtURL:file error:nil];
  if (!saved) {
    return FBResponseWithObject(@{@"saved": @NO, @"status": FBMobsterPhotosStatus(status),
                                  @"error": failure.localizedDescription ?: (finished ? @"Photos refused the file" : @"Photos didn't answer")});
  }
  return FBResponseWithObject(@{@"saved": @YES, @"status": FBMobsterPhotosStatus(status),
                                @"localIdentifier": identifier ?: @""});
}
"""  # noqa: E501
# Mobster's transfer routes (phone_io/photos.py, transfer.py): a file in pieces, checked by SHA-256 on the phone before
# Photos sees it, saved into an album; how many photos and videos the library holds; the newest items listed; and an
# item's original read back to the Mac in pieces with its SHA-256. A separate patch anchored on the Photos patch's own
# lines, so a clone already patched for Photos takes it without a fresh checkout. A runner without these answers 404
# and photos.py falls back to POST /mobster/photos (one piece, at most 100 MB, no album).
WDA_TRANSFER_ROUTE_ANCHOR = "    // Mobster: save media into Photos (send_media).\n"
WDA_TRANSFER_ROUTES = """\
    // Mobster: transfers (phone_io/transfer.py).
    [[FBRoute GET:@"/mobster/photos/count"] respondWithTarget:self action:@selector(handleMobsterPhotosCount:)],
    [[FBRoute GET:@"/mobster/photos/count"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosCount:)],
    [[FBRoute POST:@"/mobster/photos/chunk"] respondWithTarget:self action:@selector(handleMobsterPhotosChunk:)],
    [[FBRoute POST:@"/mobster/photos/chunk"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosChunk:)],
    [[FBRoute POST:@"/mobster/photos/save"] respondWithTarget:self action:@selector(handleMobsterPhotosSave:)],
    [[FBRoute POST:@"/mobster/photos/save"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosSave:)],
    [[FBRoute POST:@"/mobster/photos/recent"] respondWithTarget:self action:@selector(handleMobsterPhotosRecent:)],
    [[FBRoute POST:@"/mobster/photos/recent"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosRecent:)],
    [[FBRoute POST:@"/mobster/photos/export"] respondWithTarget:self action:@selector(handleMobsterPhotosExport:)],
    [[FBRoute POST:@"/mobster/photos/export"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosExport:)],
"""  # noqa: E501
WDA_TRANSFER_IMPORT_ANCHOR = "#import <Photos/Photos.h>\n"
WDA_TRANSFER_IMPORT = "#import <Photos/Photos.h>\n#import <CommonCrypto/CommonDigest.h>\n"
# The Photos commands' last lines: the transfer commands follow them, so FBMobsterPhotosStatus is already declared.
WDA_TRANSFER_COMMAND_ANCHOR = WDA_PHOTOS_COMMANDS[WDA_PHOTOS_COMMANDS.rindex(
    "  return FBResponseWithObject(@{@\"saved\": @YES"):]
WDA_TRANSFER_COMMANDS = """
// Mobster: transfers (phone_io/transfer.py). Pieces of at most a few MB go to a file in the runner's temporary folder;
// the save checks the whole file's SHA-256 against the Mac's before Photos sees it.
static NSString *FBMobsterTransferFolder(void)
{
  NSString *folder = [NSTemporaryDirectory() stringByAppendingPathComponent:@"mobster-transfer"];
  [NSFileManager.defaultManager createDirectoryAtPath:folder withIntermediateDirectories:YES attributes:nil error:nil];
  return folder;
}

static BOOL FBMobsterPlain(id value, NSString *pattern)
{
  if (![value isKindOfClass:NSString.class] || 0 == [(NSString *)value length]) {
    return NO;
  }
  NSRegularExpression *plain = [NSRegularExpression regularExpressionWithPattern:pattern options:(NSRegularExpressionOptions)0 error:nil];
  return nil != [plain firstMatchInString:value options:(NSMatchingOptions)0 range:NSMakeRange(0, [(NSString *)value length])];
}

static NSString *FBMobsterSHA256(NSString *path)
{
  NSInputStream *stream = [NSInputStream inputStreamWithFileAtPath:path];
  if (nil == stream) {
    return nil;
  }
  [stream open];
  CC_SHA256_CTX context;
  CC_SHA256_Init(&context);
  NSMutableData *buffer = [NSMutableData dataWithLength:(1 << 20)];
  NSInteger count = 0;
  while ((count = [stream read:(uint8_t *)buffer.mutableBytes maxLength:buffer.length]) > 0) {
    CC_SHA256_Update(&context, buffer.bytes, (CC_LONG)count);
  }
  [stream close];
  if (count < 0) {
    return nil;
  }
  unsigned char digest[CC_SHA256_DIGEST_LENGTH];
  CC_SHA256_Final(digest, &context);
  NSMutableString *hex = [NSMutableString stringWithCapacity:CC_SHA256_DIGEST_LENGTH * 2];
  for (int index = 0; index < CC_SHA256_DIGEST_LENGTH; index++) {
    [hex appendFormat:@"%02x", digest[index]];
  }
  return hex;
}

static BOOL FBMobsterPhotosAllowed(PHAuthorizationStatus status)
{
  return PHAuthorizationStatusAuthorized == status || PHAuthorizationStatusLimited == status;
}

+ (id<FBResponsePayload>)handleMobsterPhotosCount:(FBRouteRequest *)request
{
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  if (!FBMobsterPhotosAllowed(status)) {
    return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status)});
  }
  NSUInteger photos = [PHAsset fetchAssetsWithMediaType:PHAssetMediaTypeImage options:nil].count;
  NSUInteger videos = [PHAsset fetchAssetsWithMediaType:PHAssetMediaTypeVideo options:nil].count;
  return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"photos": @(photos), @"videos": @(videos)});
}

+ (id<FBResponsePayload>)handleMobsterPhotosChunk:(FBRouteRequest *)request
{
  id upload = request.arguments[@"upload"];
  id offset = request.arguments[@"offset"];
  id encoded = request.arguments[@"data"];
  if (!FBMobsterPlain(upload, @"^[a-f0-9]{8,32}$") || ![offset isKindOfClass:NSNumber.class]
      || ![encoded isKindOfClass:NSString.class]) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"upload, offset and data are required" traceback:nil]);
  }
  NSData *data = [[NSData alloc] initWithBase64EncodedString:encoded options:NSDataBase64DecodingIgnoreUnknownCharacters];
  if (nil == data) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"data is not base64" traceback:nil]);
  }
  NSFileManager *files = NSFileManager.defaultManager;
  NSString *path = [FBMobsterTransferFolder() stringByAppendingPathComponent:[@"upload-" stringByAppendingString:upload]];
  unsigned long long start = [(NSNumber *)offset unsignedLongLongValue];
  if (0 == start) {
    [files removeItemAtPath:path error:nil];
    [files createFileAtPath:path contents:nil attributes:nil];
  }
  unsigned long long have = [[files attributesOfItemAtPath:path error:nil] fileSize];
  if (![files fileExistsAtPath:path] || start != have) {
    // Out of step (a piece lost or sent twice): say where the file stands; the Mac starts again from there.
    return FBResponseWithObject(@{@"ok": @NO, @"bytes": @(have), @"error": @"offset"});
  }
  NSFileHandle *handle = [NSFileHandle fileHandleForWritingAtPath:path];
  NSError *error = nil;
  BOOL wrote = nil != handle && [handle seekToEndReturningOffset:nil error:&error] && [handle writeData:data error:&error];
  [handle closeAndReturnError:nil];
  if (!wrote) {
    [files removeItemAtPath:path error:nil];
    return FBResponseWithObject(@{@"ok": @NO, @"bytes": @0, @"error": error.localizedDescription ?: @"write"});
  }
  return FBResponseWithObject(@{@"ok": @YES, @"bytes": @(have + data.length)});
}

+ (id<FBResponsePayload>)handleMobsterPhotosSave:(FBRouteRequest *)request
{
  id upload = request.arguments[@"upload"];
  id kind = request.arguments[@"kind"];
  id filename = request.arguments[@"filename"];
  id album = request.arguments[@"album"];
  id expected = request.arguments[@"sha256"];
  id dateNow = request.arguments[@"dateNow"];
  NSNumber *wait = [request.arguments[@"authorizationTimeout"] isKindOfClass:NSNumber.class]
    ? request.arguments[@"authorizationTimeout"] : @60;
  if (!FBMobsterPlain(upload, @"^[a-f0-9]{8,32}$") || !FBMobsterPlain(expected, @"^[a-f0-9]{64}$")
      || !([kind isEqual:@"photo"] || [kind isEqual:@"video"])) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"upload, sha256 and kind (photo or video) are required" traceback:nil]);
  }
  NSString *path = [FBMobsterTransferFolder() stringByAppendingPathComponent:[@"upload-" stringByAppendingString:upload]];
  NSString *digest = FBMobsterSHA256(path);
  unsigned long long size = [[NSFileManager.defaultManager attributesOfItemAtPath:path error:nil] fileSize];
  if (nil == digest || ![digest isEqualToString:expected]) {
    // What arrived isn't what the Mac sent: Photos never sees it.
    [NSFileManager.defaultManager removeItemAtPath:path error:nil];
    return FBResponseWithObject(@{@"saved": @NO, @"error": @"checksum", @"sha256": digest ?: @"", @"bytes": @(size)});
  }
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  if (PHAuthorizationStatusNotDetermined == status) {
    __block PHAuthorizationStatus answered = PHAuthorizationStatusNotDetermined;
    __block BOOL asked = NO;
    [PHPhotoLibrary requestAuthorizationForAccessLevel:PHAccessLevelReadWrite handler:^(PHAuthorizationStatus value) {
      answered = value;
      asked = YES;
    }];
    [[[[FBRunLoopSpinner new] timeout:MAX(1.0, MIN(wait.doubleValue, 120.0))] interval:0.2] spinUntilTrue:^BOOL{
      return asked;
    }];
    status = asked ? answered : PHAuthorizationStatusNotDetermined;
  }
  if (!FBMobsterPhotosAllowed(status)) {
    return FBResponseWithObject(@{@"saved": @NO, @"status": FBMobsterPhotosStatus(status), @"sha256": digest});
  }
  __block NSString *identifier = nil;
  __block BOOL finished = NO;
  __block BOOL saved = NO;
  __block NSError *failure = nil;
  NSURL *file = [NSURL fileURLWithPath:path];
  [[PHPhotoLibrary sharedPhotoLibrary] performChanges:^{
    PHAssetCreationRequest *creation = [PHAssetCreationRequest creationRequestForAsset];
    PHAssetResourceCreationOptions *options = [PHAssetResourceCreationOptions new];
    options.shouldMoveFile = YES;
    if ([filename isKindOfClass:NSString.class] && [(NSString *)filename length] > 0) {
      options.originalFilename = filename;
    }
    [creation addResourceWithType:([kind isEqual:@"video"] ? PHAssetResourceTypeVideo : PHAssetResourceTypePhoto)
                          fileURL:file
                          options:options];
    if ([dateNow isEqual:@YES]) {
      // Dated now in the library (the file itself is unchanged), so it is first in every app's Recents.
      creation.creationDate = [NSDate date];
    }
    identifier = creation.placeholderForCreatedAsset.localIdentifier;
  } completionHandler:^(BOOL success, NSError *changeError) {
    saved = success;
    failure = changeError;
    finished = YES;
  }];
  [[[[FBRunLoopSpinner new] timeout:600] interval:0.1] spinUntilTrue:^BOOL{
    return finished;
  }];
  [NSFileManager.defaultManager removeItemAtPath:path error:nil];
  if (!saved) {
    return FBResponseWithObject(@{@"saved": @NO, @"status": FBMobsterPhotosStatus(status), @"sha256": digest,
                                  @"error": failure.localizedDescription ?: (finished ? @"Photos refused the file" : @"Photos didn't answer")});
  }
  // The album is a second change: the item is in Photos whatever the album step says.
  NSString *albumError = @"";
  if ([album isKindOfClass:NSString.class] && [(NSString *)album length] > 0 && identifier.length > 0) {
    PHFetchOptions *named = [PHFetchOptions new];
    named.predicate = [NSPredicate predicateWithFormat:@"title == %@", album];
    PHAssetCollection *collection = [PHAssetCollection fetchAssetCollectionsWithType:PHAssetCollectionTypeAlbum
                                                                             subtype:PHAssetCollectionSubtypeAlbumRegular
                                                                             options:named].firstObject;
    PHFetchResult<PHAsset *> *assets = [PHAsset fetchAssetsWithLocalIdentifiers:@[identifier] options:nil];
    __block BOOL filed = NO;
    __block BOOL filedDone = NO;
    __block NSError *fileError = nil;
    [[PHPhotoLibrary sharedPhotoLibrary] performChanges:^{
      PHAssetCollectionChangeRequest *change = nil != collection
        ? [PHAssetCollectionChangeRequest changeRequestForAssetCollection:collection]
        : [PHAssetCollectionChangeRequest creationRequestForAssetCollectionWithTitle:album];
      [change addAssets:assets];
    } completionHandler:^(BOOL success, NSError *changeError) {
      filed = success;
      fileError = changeError;
      filedDone = YES;
    }];
    [[[[FBRunLoopSpinner new] timeout:60] interval:0.1] spinUntilTrue:^BOOL{
      return filedDone;
    }];
    if (!filed) {
      albumError = fileError.localizedDescription ?: @"Photos didn't add it to the album";
    }
  }
  return FBResponseWithObject(@{@"saved": @YES, @"status": FBMobsterPhotosStatus(status),
                                @"localIdentifier": identifier ?: @"", @"sha256": digest, @"bytes": @(size),
                                @"albumError": albumError});
}

static PHAssetResource *FBMobsterOriginal(PHAsset *asset)
{
  NSArray<PHAssetResource *> *resources = [PHAssetResource assetResourcesForAsset:asset];
  PHAssetResourceType wanted = PHAssetMediaTypeVideo == asset.mediaType ? PHAssetResourceTypeVideo : PHAssetResourceTypePhoto;
  for (PHAssetResource *resource in resources) {
    if (resource.type == wanted) {
      return resource;
    }
  }
  return resources.firstObject;
}

+ (id<FBResponsePayload>)handleMobsterPhotosRecent:(FBRouteRequest *)request
{
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  if (!FBMobsterPhotosAllowed(status)) {
    return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"items": @[]});
  }
  NSInteger limit = [request.arguments[@"limit"] isKindOfClass:NSNumber.class] ? [request.arguments[@"limit"] integerValue] : 1;
  PHFetchOptions *options = [PHFetchOptions new];
  options.sortDescriptors = @[[NSSortDescriptor sortDescriptorWithKey:@"creationDate" ascending:NO]];
  options.fetchLimit = (NSUInteger)MAX(1, MIN(limit, 50));
  NSPredicate *media = [NSPredicate predicateWithFormat:@"mediaType == %d OR mediaType == %d", PHAssetMediaTypeImage, PHAssetMediaTypeVideo];
  id since = request.arguments[@"since"];
  options.predicate = [since isKindOfClass:NSNumber.class]
    ? [NSCompoundPredicate andPredicateWithSubpredicates:@[media, [NSPredicate predicateWithFormat:@"creationDate >= %@", [NSDate dateWithTimeIntervalSince1970:[since doubleValue]]]]]
    : media;
  PHFetchResult<PHAsset *> *assets = [PHAsset fetchAssetsWithOptions:options];
  NSMutableArray *items = [NSMutableArray array];
  [assets enumerateObjectsUsingBlock:^(PHAsset *asset, NSUInteger index, BOOL *stop) {
    PHAssetResource *original = FBMobsterOriginal(asset);
    [items addObject:@{@"id": asset.localIdentifier ?: @"",
                       @"filename": original.originalFilename ?: @"",
                       @"kind": PHAssetMediaTypeVideo == asset.mediaType ? @"video" : @"photo",
                       @"created": @(asset.creationDate.timeIntervalSince1970),
                       @"width": @(asset.pixelWidth), @"height": @(asset.pixelHeight),
                       @"duration": @(asset.duration)}];
  }];
  return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"items": items});
}

static NSMutableDictionary<NSString *, NSDictionary *> *FBMobsterExports(void)
{
  static NSMutableDictionary *exports = nil;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    exports = [NSMutableDictionary dictionary];
  });
  return exports;
}

+ (id<FBResponsePayload>)handleMobsterPhotosExport:(FBRouteRequest *)request
{
  id identifier = request.arguments[@"id"];
  id offset = request.arguments[@"offset"];
  id length = request.arguments[@"length"];
  if (![identifier isKindOfClass:NSString.class] || 0 == [(NSString *)identifier length] || [(NSString *)identifier length] > 200
      || ![offset isKindOfClass:NSNumber.class] || ![length isKindOfClass:NSNumber.class]) {
    return FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@"id, offset and length are required" traceback:nil]);
  }
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  if (!FBMobsterPhotosAllowed(status)) {
    return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"error": @"status"});
  }
  NSMutableDictionary *exports = FBMobsterExports();
  NSDictionary *export = exports[identifier];
  if (0 == [(NSNumber *)offset unsignedLongLongValue] || nil == export) {
    if (nil != export) {
      [NSFileManager.defaultManager removeItemAtPath:export[@"path"] error:nil];
      [exports removeObjectForKey:identifier];
    }
    PHAsset *asset = [PHAsset fetchAssetsWithLocalIdentifiers:@[identifier] options:nil].firstObject;
    PHAssetResource *original = nil == asset ? nil : FBMobsterOriginal(asset);
    if (nil == original) {
      return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"error": @"not_found"});
    }
    NSString *path = [FBMobsterTransferFolder() stringByAppendingPathComponent:[@"export-" stringByAppendingString:NSUUID.UUID.UUIDString]];
    PHAssetResourceRequestOptions *options = [PHAssetResourceRequestOptions new];
    options.networkAccessAllowed = YES;  // an original kept only in iCloud (Optimize iPhone Storage) is fetched first
    __block BOOL written = NO;
    __block BOOL writeDone = NO;
    __block NSError *writeError = nil;
    [[PHAssetResourceManager defaultManager] writeDataForAssetResource:original toFile:[NSURL fileURLWithPath:path]
                                                               options:options completionHandler:^(NSError *error) {
      written = nil == error;
      writeError = error;
      writeDone = YES;
    }];
    [[[[FBRunLoopSpinner new] timeout:900] interval:0.1] spinUntilTrue:^BOOL{
      return writeDone;
    }];
    NSString *digest = written ? FBMobsterSHA256(path) : nil;
    if (nil == digest) {
      [NSFileManager.defaultManager removeItemAtPath:path error:nil];
      return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"error": writeError.localizedDescription ?: @"export"});
    }
    unsigned long long size = [[NSFileManager.defaultManager attributesOfItemAtPath:path error:nil] fileSize];
    export = @{@"path": path, @"sha256": digest, @"bytes": @(size), @"filename": original.originalFilename ?: @""};
    exports[identifier] = export;
  }
  unsigned long long start = [(NSNumber *)offset unsignedLongLongValue];
  unsigned long long size = [export[@"bytes"] unsignedLongLongValue];
  // length 0: only the size and the checksum (to check an item that arrived another way), and the copy goes.
  NSUInteger count = (NSUInteger)MIN([(NSNumber *)length unsignedLongLongValue], (unsigned long long)(16 << 20));
  NSString *exportPath = export[@"path"];
  NSData *data = [NSData data];
  if (count > 0) {
    NSFileHandle *handle = [NSFileHandle fileHandleForReadingAtPath:exportPath];
    data = nil;
    if (nil != handle && start <= size && [handle seekToOffset:start error:nil]) {
      data = [handle readDataUpToLength:count error:nil];
    }
    [handle closeAndReturnError:nil];
  }
  if (nil == data) {
    return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"error": @"read"});
  }
  BOOL done = 0 == count || start + data.length >= size;
  if (done) {
    [NSFileManager.defaultManager removeItemAtPath:exportPath error:nil];
    [exports removeObjectForKey:identifier];
  }
  return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"filename": export[@"filename"],
                                @"bytes": export[@"bytes"], @"sha256": export[@"sha256"], @"offset": @(start),
                                @"data": [data base64EncodedStringWithOptions:0], @"done": @(done)});
}
"""  # noqa: E501
TRANSFER_COMMANDS_PATCH = ("WebDriverAgentLib/Commands/FBCustomCommands.m", WDA_TRANSFER_COMMAND_ANCHOR,
                           WDA_TRANSFER_COMMAND_ANCHOR + WDA_TRANSFER_COMMANDS)
# Two routes for the content queue's checks (phone_ready.py), applied only with MOBSTER_QUEUE_PREFLIGHT=1: without it
# the patch set, its digest and the runner stay exactly as they were. POST /mobster/photos/authorize asks iOS for Photos
# access and saves nothing: the person answers on the phone, and Mobster never presses the grant
# (system_dialogs.GRANTS).
# It asks only while the status is not_determined, so it never re-asks and never resets an answer. GET
# /mobster/power says whether Low Power Mode is on (the phone locks itself after 30 seconds), and nothing else. Both
# are anchored on stock WebDriverAgent lines that no other patch uses, so they apply beside the Photos and transfer
# routes.
PREFLIGHT_ROUTE_ANCHOR = ("    [[FBRoute GET:@\"/wda/voiceOver/enabled\"].withoutSession respondWithTarget:self "
                          "action:@selector(handleVoiceOverEnabled:)],\n")
PREFLIGHT_ROUTES = """\
    // Mobster: the content queue's checks (phone_ready.py).
    [[FBRoute POST:@"/mobster/photos/authorize"] respondWithTarget:self action:@selector(handleMobsterPhotosAuthorize:)],
    [[FBRoute POST:@"/mobster/photos/authorize"].withoutSession respondWithTarget:self action:@selector(handleMobsterPhotosAuthorize:)],
    [[FBRoute GET:@"/mobster/power"] respondWithTarget:self action:@selector(handleMobsterPower:)],
    [[FBRoute GET:@"/mobster/power"].withoutSession respondWithTarget:self action:@selector(handleMobsterPower:)],
"""  # noqa: E501
PREFLIGHT_COMMAND_ANCHOR = "+ (id<FBResponsePayload>)handleDismissKeyboardCommand:(FBRouteRequest *)request\n"
PREFLIGHT_COMMANDS = """\
// Mobster: ask for Photos access without saving anything (phone_ready.py). Only a status that is still not_determined
// is asked about, so an answer the person gave is never asked for again and never changed. The person answers on the
// iPhone; this waits for that answer up to authorizationTimeout seconds and then says where it stands.
+ (id<FBResponsePayload>)handleMobsterPhotosAuthorize:(FBRouteRequest *)request
{
  NSNumber *wait = [request.arguments[@"authorizationTimeout"] isKindOfClass:NSNumber.class]
    ? request.arguments[@"authorizationTimeout"] : @20;
  PHAuthorizationStatus status = [PHPhotoLibrary authorizationStatusForAccessLevel:PHAccessLevelReadWrite];
  BOOL asked = NO;
  if (PHAuthorizationStatusNotDetermined == status) {
    asked = YES;
    __block PHAuthorizationStatus answered = PHAuthorizationStatusNotDetermined;
    __block BOOL finished = NO;
    [PHPhotoLibrary requestAuthorizationForAccessLevel:PHAccessLevelReadWrite handler:^(PHAuthorizationStatus value) {
      answered = value;
      finished = YES;
    }];
    [[[[FBRunLoopSpinner new] timeout:MAX(1.0, MIN(wait.doubleValue, 120.0))] interval:0.2] spinUntilTrue:^BOOL{
      return finished;
    }];
    status = finished ? answered : PHAuthorizationStatusNotDetermined;
  }
  return FBResponseWithObject(@{@"status": FBMobsterPhotosStatus(status), @"asked": @(asked)});
}

// Mobster: whether Low Power Mode is on (phone_ready.py). It reads one process flag and changes nothing.
+ (id<FBResponsePayload>)handleMobsterPower:(FBRouteRequest *)request
{
  return FBResponseWithObject(@{@"lowPowerMode": @(NSProcessInfo.processInfo.isLowPowerModeEnabled)});
}

"""  # noqa: E501
# The chunked save keeps the upload in a file named upload-<id> with no extension, and PhotoKit refused it with
# PHPhotosErrorInvalidResource (3302) on an iOS 26.4 simulator under Limit Access (19 Oct 2026), while the one-piece
# route, whose file has an extension, saved. The Mac sends the extension with the save, so with the flag on the file is
# renamed to
# carry it first. It is the transfer patch's own text with those lines added (a variant, not an edit of its output), so
# every patch still applies to the unpatched file and again, unchanged, to a patched one.
PREFLIGHT_SAVE_ANCHOR = "  NSURL *file = [NSURL fileURLWithPath:path];\n"
PREFLIGHT_SAVE_NAMED = """\
  id suffix = request.arguments[@"extension"];
  NSString *extension = FBMobsterPlain(suffix, @"^[A-Za-z0-9]{1,5}$") ? suffix : ([kind isEqual:@"video"] ? @"mov" : @"jpg");
  NSString *named = [path stringByAppendingPathExtension:extension];
  [NSFileManager.defaultManager removeItemAtPath:named error:nil];
  if ([NSFileManager.defaultManager moveItemAtPath:path toPath:named error:nil]) {
    path = named;
  }
"""  # noqa: E501
assert WDA_TRANSFER_COMMANDS.count(PREFLIGHT_SAVE_ANCHOR) == 1
TRANSFER_COMMANDS_NAMED_PATCH = (
    TRANSFER_COMMANDS_PATCH[0], TRANSFER_COMMANDS_PATCH[1],
    TRANSFER_COMMANDS_PATCH[1] + WDA_TRANSFER_COMMANDS.replace(PREFLIGHT_SAVE_ANCHOR,
                                                              PREFLIGHT_SAVE_NAMED + PREFLIGHT_SAVE_ANCHOR, 1))
PREFLIGHT_PATCHES = (
    ("WebDriverAgentLib/Commands/FBCustomCommands.m", PREFLIGHT_ROUTE_ANCHOR,
     PREFLIGHT_ROUTE_ANCHOR + PREFLIGHT_ROUTES),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m", PREFLIGHT_COMMAND_ANCHOR,
     PREFLIGHT_COMMANDS + PREFLIGHT_COMMAND_ANCHOR),
)
# The CLI password route (/mobster/secret) never logs its body and types through no unredacted XCTest event. The
# target must already be focused and empty; validation and the typing stay together on WDA's main queue, and the
# route never taps, clears, submits, retries or unlocks a phone.
WDA_SECRET_COMMANDS = r'''
+ (id<FBResponsePayload>)handleMobsterSecret:(FBRouteRequest *)request
{
  // Never include request arguments or exception descriptions in this route's response.
  @try {
    NSString *bundle = request.arguments[@"bundleId"];
    NSString *identifier = request.arguments[@"identifier"];
    BOOL checkOnly = [request.URL.path.lowercaseString hasSuffix:@"/check"];
    NSString *text = request.arguments[@"secret"];
    if (![bundle isKindOfClass:NSString.class] || bundle.length == 0 ||
        [bundle isEqualToString:@"com.apple.springboard"] ||
        ![identifier isKindOfClass:NSString.class] || identifier.length == 0) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"target"});
    }
    if (!checkOnly && (![text isKindOfClass:NSString.class] || text.length == 0 ||
        text.length > 256 || [text rangeOfCharacterFromSet:NSCharacterSet.controlCharacterSet].location != NSNotFound ||
        [text rangeOfCharacterFromSet:NSCharacterSet.newlineCharacterSet].location != NSNotFound)) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"secret"});
    }
    XCUIApplication *app = XCUIApplication.fb_activeApplication;
    if (XCUIDevice.sharedDevice.fb_isScreenLocked || ![app.bundleID isEqualToString:bundle]) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"foreground"});
    }
    NSArray<XCUIElement *> *matches = [[app.secureTextFields matchingIdentifier:identifier] allElementsBoundByIndex];
    if (matches.count != 1) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"target"});
    }
    XCUIElement *field = matches.firstObject;
    if (!field.enabled || !field.hittable) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"field"});
    }
    // Resolve focus last. The device owner can still change focus outside WDA's queue.
    id<FBXCElementSnapshot> snapshot = [field fb_standardSnapshot];
    id value = snapshot.value;
    BOOL empty = value == nil || ([value isKindOfClass:NSString.class] &&
      ([(NSString *)value length] == 0 || [value isEqual:snapshot.placeholderValue]));
    if (snapshot.elementType != XCUIElementTypeSecureTextField || !snapshot.hasKeyboardFocus || !empty) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"field"});
    }
    // Snapshot reads can yield to the app or lock screen; verify the foreground afterward.
    if (XCUIDevice.sharedDevice.fb_isScreenLocked ||
        ![XCUIApplication.fb_activeApplication.bundleID isEqualToString:bundle]) {
      return FBResponseWithObject(@{@"ok": @NO, @"code": @"foreground"});
    }
    if (checkOnly) {
      return FBResponseWithObject(@{@"ok": @YES, @"version": @1});
    }
    XCSynthesizedEventRecord *event = [[XCSynthesizedEventRecord alloc] initWithName:@"Mobster private text entry"];
    XCPointerEventPath *keys = [[XCPointerEventPath alloc] initForTextInput];
    // A fixed 60 keys/s keeps even the maximum input below the completion deadline.
    [keys typeText:text atOffset:0.0 typingSpeed:60 shouldRedact:YES];
    [event addPointerEventPath:keys];
    // Upstream's helper ignores the completion BOOL and waits without a deadline.
    // Preserve an uncertain result instead of claiming success or resending input.
    NSObject *state = [NSObject new];
    __block BOOL finished = NO;
    __block BOOL sent = NO;
    [XCUIDevice.sharedDevice.eventSynthesizer synthesizeEvent:event completion:^(BOOL result, NSError *error) {
      @synchronized (state) {
        sent = result && error == nil;
        finished = YES;
      }
    }];
    BOOL completed = [[[FBRunLoopSpinner new] timeout:20] spinUntilTrue:^BOOL {
      @synchronized (state) { return finished; }
    }];
    BOOL confirmed;
    @synchronized (state) { confirmed = completed && sent; }
    return FBResponseWithObject(@{@"ok": @(confirmed), @"code": confirmed ? @"sent" : @"unknown"});
  } @catch (NSException *exception) {
    (void)exception;
    return FBResponseWithObject(@{@"ok": @NO, @"code": @"unknown"});
  }
}
'''
SECRET_PATCHES = (
    ("WebDriverAgentLib/Routing/FBWebServer.m",
     "        [FBLogger verboseLog:routeParams.description];",
     "        // Mobster private input: never log its body, even with verbose logging enabled.\n"
     "        if (![routeParams.URL.path.lowercaseString containsString:@\"/mobster/secret\"]) {\n"
     "          [FBLogger verboseLog:routeParams.description];\n"
     "        }"),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m",
     '#import "FBCustomCommands.h"\n',
     '#import "FBCustomCommands.h"\n'
     '#import "XCUIElement+FBUtilities.h"\n'
     '#import "FBXCElementSnapshot.h"\n'
     '#import "XCUIDevice.h"\n'
     '#import "XCSynthesizedEventRecord.h"\n'
     '#import "XCPointerEventPath.h"\n'),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m",
     '    [[FBRoute POST:@"/timeouts"] respondWithTarget:self action:@selector(handleTimeouts:)],',
     '    [[FBRoute POST:@"/mobster/secret/check"] respondWithTarget:self action:@selector(handleMobsterSecret:)],\n'
     '    [[FBRoute POST:@"/mobster/secret"] respondWithTarget:self action:@selector(handleMobsterSecret:)],\n'
     '    [[FBRoute POST:@"/timeouts"] respondWithTarget:self action:@selector(handleTimeouts:)],'),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m",
     "@implementation FBCustomCommands\n",
     "@implementation FBCustomCommands\n" + WDA_SECRET_COMMANDS),
)
# Idempotent (file, anchor, patched) edits: every build applies them to the clone.
WDA_PATCHES = (
    # The MJPEG socket binds USE_IP like the API; upstream it always listens on every interface.
    ("WebDriverAgentLib/Routing/FBWebServer.m",
     "initWithPort:(uint16_t)FBConfiguration.sharedInstance.mjpegServerPort];\n",
     "initWithPort:(uint16_t)FBConfiguration.sharedInstance.mjpegServerPort];\n"
     "  // Mobster: the MJPEG socket listens where the API does (USE_IP), not on every interface.\n"
     "  self.screenshotsBroadcaster.interface = FBConfiguration.sharedInstance.bindingIPAddress;\n"),
    ("WebDriverAgentLib/Routing/FBHTTPServer.m",
     "static const NSUInteger FBMaxRequestHeaderSize = 16 * 1024;\n",
     "static const NSUInteger FBMaxRequestHeaderSize = 16 * 1024;\n" + WDA_LOCAL_HOST),
    ("WebDriverAgentLib/Routing/FBHTTPServer.m",
     "  NSUInteger contentLength = 0;\n  if (![self resolveBodyLength:",
     "  if (nil != requestHeaders[@\"origin\"] || !FBMobsterLocalHost(requestHeaders[@\"host\"])) {\n"
     "    RouteResponse *forbidden = [RouteResponse new];\n"
     "    [FBResponseWithStatus([FBCommandStatus invalidArgumentErrorWithMessage:@\"Local clients only\" "
     "traceback:nil])\n"
     "     dispatchWithResponse:forbidden];\n"
     "    [self failClient:client withResponse:forbidden];\n"
     "    return nil;\n"
     "  }\n"
     "  NSUInteger contentLength = 0;\n  if (![self resolveBodyLength:"),
    ("WebDriverAgentLib/Utilities/FBMjpegServer.m",
     "static NSString *const SERVER_NAME = @\"WDA MJPEG Server\";\n",
     "static NSString *const SERVER_NAME = @\"WDA MJPEG Server\";\n" + WDA_LOCAL_HOST + WDA_LOCAL_STREAM),
    ("WebDriverAgentLib/Utilities/FBMjpegServer.m",
     "  [FBLogger log:@\"Starting screenshots broadcast for the client\"];\n",
     "  if (!FBMobsterLocalStreamRequest(data)) {\n"
     "    [FBLogger log:@\"Refused a screenshots broadcast request from a browser\"];\n"
     "    nw_connection_cancel(client);\n"
     "    return;\n"
     "  }\n"
     "  [FBLogger log:@\"Starting screenshots broadcast for the client\"];\n"),
    # Photos (send_media): the framework, the routes and their commands.
    ("WebDriverAgentLib/Commands/FBCustomCommands.m",
     "#import <CoreLocation/CoreLocation.h>\n",
     "#import <CoreLocation/CoreLocation.h>\n#import <Photos/Photos.h>\n"),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m",
     "    [[FBRoute OPTIONS:@\"/*\"].withoutSession respondWithTarget:self action:@selector(handlePingCommand:)],\n",
     WDA_PHOTOS_ROUTES
     + "    [[FBRoute OPTIONS:@\"/*\"].withoutSession respondWithTarget:self action:@selector(handlePingCommand:)],\n"),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m",
     "#pragma mark - Commands\n",
     "#pragma mark - Commands\n" + WDA_PHOTOS_COMMANDS),
    # Transfers (phone_io/transfer.py): anchored on the Photos patch's own lines, applied after it.
    ("WebDriverAgentLib/Commands/FBCustomCommands.m", WDA_TRANSFER_IMPORT_ANCHOR, WDA_TRANSFER_IMPORT),
    ("WebDriverAgentLib/Commands/FBCustomCommands.m", WDA_TRANSFER_ROUTE_ANCHOR,
     WDA_TRANSFER_ROUTES + WDA_TRANSFER_ROUTE_ANCHOR),
    TRANSFER_COMMANDS_PATCH,
# the gate for screenshots and snapshots (the MJPEG/AX hang); private password entry
) + wda_gate.PATCHES + SECRET_PATCHES


def active_patches(env=None):
    """WDA_PATCHES; with MOBSTER_QUEUE_PREFLIGHT on, the queue-check routes (PREFLIGHT_PATCHES) too, and the transfer
    commands in their variant that names the upload's file with its extension."""
    if not phone_ready.enabled(env):
        return WDA_PATCHES
    return tuple(TRANSFER_COMMANDS_NAMED_PATCH if patch == TRANSFER_COMMANDS_PATCH else patch
                 for patch in WDA_PATCHES) + PREFLIGHT_PATCHES


def patch_digest():
    """A fingerprint of every edit active_patches makes; a runner built with another may run unpatched, so isn't used.
    Turning MOBSTER_QUEUE_PREFLIGHT on or off changes it, so each phone rebuilds its helper once (about 115 s)."""
    return hashlib.sha256(json.dumps(active_patches(), separators=(",", ":")).encode()).hexdigest()[:16]


def pristine(project, name, git=None):
    """``name`` as the checked-out release has it (``git show HEAD:name``), or None when git can't say."""
    from .. import mac_tools
    git = git or mac_tools.tool("git") or "git"
    try:
        result = subprocess.run([git, "-C", str(project), "show", f"HEAD:{name}"], capture_output=True, timeout=20,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def patch_wda(project, git=None):
    """Apply active_patches() to a clone: the patched files become exactly the pinned release's with these patches,
    however
    an earlier Mobster left them. A source they don't fit fails the build rather than run open.

    Each file starts from the release's own text (``pristine``: git HEAD), never from what is on disk: the clone is
    shared and kept between builds, and a patch whose text changed in an update would otherwise go in beside the old
    one (two ``handleMobsterSecret:`` methods, a build that fails, 16 Oct). The edits are made in memory and written
    only when every one fits. A tree that isn't a git checkout is patched from disk, and one an earlier Mobster patched
    differently (it names Mobster, but this patch isn't there) is refused rather than patched twice."""
    project = Path(project)
    refused = RuntimeError(f"WebDriverAgent is not {wda_source.REF}, so it can't be limited to USB. "
                           f"Delete {project} and build again.")
    patches = active_patches()
    texts, from_disk = {}, set()
    for name, _anchor, _patched in patches:
        if name in texts:
            continue
        base = pristine(project, name, git)
        if base is None:
            base = (project / name).read_text()
            from_disk.add(name)
        texts[name] = base
    # A file read from disk that already names Mobster was patched by a Mobster before this run.
    patched_before = {name for name in from_disk if "mobster" in texts[name].lower()}
    for name, anchor, patched in patches:
        text = texts[name]
        if patched in text:
            continue
        if name in patched_before:
            raise refused   # patched before with other edits: applying these too would duplicate them
        if text.count(anchor) != 1:
            raise refused
        texts[name] = text.replace(anchor, patched)
    for name, text in texts.items():
        path = project / name
        if path.read_text() != text:
            path.write_text(text)


def xctestrun_targets(plan):
    """Test targets in either Xcode test-plan format; metadata is never a target."""
    if not isinstance(plan, dict):
        return []
    configurations = plan.get("TestConfigurations")
    if isinstance(configurations, list):
        return [target for configuration in configurations if isinstance(configuration, dict)
                for target in configuration.get("TestTargets") or () if isinstance(target, dict)]
    return [value for key, value in plan.items() if not key.startswith("__") and isinstance(value, dict)]


def pin_loopback(products):
    """USE_IP=127.0.0.1 in built test plans: a build made before it was passed serves every interface."""
    for path in Path(products).glob("*.xctestrun"):
        try:
            with open(path, "rb") as stream:
                plan = plistlib.load(stream)
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        changed = False
        for target in xctestrun_targets(plan):
            env = target.get("EnvironmentVariables")
            if not isinstance(env, dict):
                env = target["EnvironmentVariables"] = {}
            for section in (env, target.get("TestingEnvironmentVariables")):
                if isinstance(section, dict) and (section is env or "USE_IP" in section) \
                        and section.get("USE_IP") != LOOPBACK:
                    section["USE_IP"] = LOOPBACK
                    changed = True
        if changed:
            with open(path, "wb") as stream:
                plistlib.dump(plan, stream)
