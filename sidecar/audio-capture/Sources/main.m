#import <CoreAudio/CoreAudio.h>
#import <AudioToolbox/AudioToolbox.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <CoreMedia/CoreMedia.h>
#import <Foundation/Foundation.h>
#import <dispatch/dispatch.h>
#import <errno.h>
#import <fcntl.h>
#import <signal.h>
#import <stdatomic.h>
#import <stdlib.h>
#import <string.h>
#import <unistd.h>

static NSString *FourCC(OSStatus status) {
    UInt32 value = (UInt32)status;
    char chars[5] = {
        (char)((value >> 24) & 0xff),
        (char)((value >> 16) & 0xff),
        (char)((value >> 8) & 0xff),
        (char)(value & 0xff),
        0,
    };
    BOOL printable = YES;
    for (int i = 0; i < 4; i++) {
        if (chars[i] < 32 || chars[i] > 126) {
            printable = NO;
            break;
        }
    }
    return printable ? [NSString stringWithUTF8String:chars] : [NSString stringWithFormat:@"0x%08x", value];
}

static NSError *MakeError(NSString *message) {
    return [NSError errorWithDomain:@"SeamAudioCapture" code:1 userInfo:@{NSLocalizedDescriptionKey: message}];
}

static NSString *SingleLine(NSString *value) {
    if (value == nil) {
        return @"";
    }
    NSString *result = [value stringByReplacingOccurrencesOfString:@"\r" withString:@" "];
    result = [result stringByReplacingOccurrencesOfString:@"\n" withString:@" "];
    return [result stringByReplacingOccurrencesOfString:@"\t" withString:@" "];
}

static BOOL CheckStatus(OSStatus status, NSString *label, NSError **error) {
    if (status == noErr) {
        return YES;
    }
    if (error != NULL) {
        *error = MakeError([NSString stringWithFormat:@"%@ failed: %d (%@)", label, status, FourCC(status)]);
    }
    return NO;
}

@protocol SeamAudioCapture <NSObject>
- (BOOL)startWithError:(NSError **)error;
- (void)stop;
@end

static NSString *DescribeFormat(AudioStreamBasicDescription format) {
    return [NSString stringWithFormat:@"rate=%.0f channels=%u bits=%u bytesPerFrame=%u formatID=%u flags=%u",
                                      format.mSampleRate,
                                      format.mChannelsPerFrame,
                                      format.mBitsPerChannel,
                                      format.mBytesPerFrame,
                                      format.mFormatID,
                                      format.mFormatFlags];
}

static NSString *GetAudioObjectStringProperty(AudioObjectID objectID, AudioObjectPropertySelector selector, AudioObjectPropertyScope scope) {
    AudioObjectPropertyAddress address = {
        .mSelector = selector,
        .mScope = scope,
        .mElement = kAudioObjectPropertyElementMain,
    };
    CFStringRef value = NULL;
    UInt32 size = sizeof(value);
    OSStatus status = AudioObjectGetPropertyData(objectID, &address, 0, NULL, &size, &value);
    if (status != noErr || value == NULL) {
        return nil;
    }
    return CFBridgingRelease(value);
}

static UInt32 GetInputChannelCount(AudioDeviceID deviceID) {
    AudioObjectPropertyAddress address = {
        .mSelector = kAudioDevicePropertyStreamConfiguration,
        .mScope = kAudioDevicePropertyScopeInput,
        .mElement = kAudioObjectPropertyElementMain,
    };
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(deviceID, &address, 0, NULL, &size) != noErr || size == 0) {
        return 0;
    }
    AudioBufferList *bufferList = (AudioBufferList *)malloc(size);
    if (bufferList == NULL) {
        return 0;
    }
    UInt32 channels = 0;
    if (AudioObjectGetPropertyData(deviceID, &address, 0, NULL, &size, bufferList) == noErr) {
        for (UInt32 i = 0; i < bufferList->mNumberBuffers; i++) {
            channels += bufferList->mBuffers[i].mNumberChannels;
        }
    }
    free(bufferList);
    return channels;
}

static AudioDeviceID GetDefaultInputDeviceID(void) {
    AudioObjectPropertyAddress address = {
        .mSelector = kAudioHardwarePropertyDefaultInputDevice,
        .mScope = kAudioObjectPropertyScopeGlobal,
        .mElement = kAudioObjectPropertyElementMain,
    };
    AudioDeviceID deviceID = kAudioObjectUnknown;
    UInt32 size = sizeof(deviceID);
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, &deviceID) != noErr) {
        return kAudioObjectUnknown;
    }
    return deviceID;
}

static NSDictionary *InputDeviceInfo(AudioDeviceID deviceID) {
    if (deviceID == kAudioObjectUnknown || GetInputChannelCount(deviceID) == 0) {
        return nil;
    }
    NSString *name = GetAudioObjectStringProperty(deviceID, kAudioObjectPropertyName, kAudioObjectPropertyScopeGlobal);
    NSString *uid = GetAudioObjectStringProperty(deviceID, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal);
    if (uid.length == 0) {
        return nil;
    }
    return @{
        @"id": @(deviceID),
        @"name": name.length ? name : uid,
        @"uid": uid,
    };
}

static NSArray<NSDictionary *> *InputDeviceInfos(void) {
    AudioObjectPropertyAddress address = {
        .mSelector = kAudioHardwarePropertyDevices,
        .mScope = kAudioObjectPropertyScopeGlobal,
        .mElement = kAudioObjectPropertyElementMain,
    };
    UInt32 size = 0;
    if (AudioObjectGetPropertyDataSize(kAudioObjectSystemObject, &address, 0, NULL, &size) != noErr || size == 0) {
        return @[];
    }
    UInt32 count = size / sizeof(AudioDeviceID);
    AudioDeviceID *devices = (AudioDeviceID *)malloc(size);
    if (devices == NULL) {
        return @[];
    }
    NSMutableArray<NSDictionary *> *result = [NSMutableArray array];
    if (AudioObjectGetPropertyData(kAudioObjectSystemObject, &address, 0, NULL, &size, devices) == noErr) {
        for (UInt32 i = 0; i < count; i++) {
            NSDictionary *info = InputDeviceInfo(devices[i]);
            if (info != nil) {
                [result addObject:info];
            }
        }
    }
    free(devices);
    return result;
}

static NSDictionary *FindInputDeviceInfo(NSString *requestedName, NSString *requestedUID) {
    NSArray<NSDictionary *> *devices = InputDeviceInfos();
    if (devices.count == 0) {
        return nil;
    }

    NSString *uid = [requestedUID stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    if (uid.length > 0) {
        for (NSDictionary *device in devices) {
            if ([device[@"uid"] isEqualToString:uid]) {
                return device;
            }
        }
    }

    NSString *name = [requestedName stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    if (name.length > 0) {
        for (NSDictionary *device in devices) {
            if ([device[@"name"] caseInsensitiveCompare:name] == NSOrderedSame) {
                return device;
            }
        }
        for (NSDictionary *device in devices) {
            NSString *candidate = device[@"name"] ?: @"";
            if ([candidate rangeOfString:name options:NSCaseInsensitiveSearch].location != NSNotFound ||
                [name rangeOfString:candidate options:NSCaseInsensitiveSearch].location != NSNotFound) {
                return device;
            }
        }
    }

    AudioDeviceID defaultID = GetDefaultInputDeviceID();
    if (defaultID != kAudioObjectUnknown) {
        for (NSDictionary *device in devices) {
            if ([device[@"id"] unsignedIntValue] == defaultID) {
                return device;
            }
        }
    }
    return devices.firstObject;
}

@interface ScreenCaptureKitAudioCapture : NSObject <SeamAudioCapture, SCStreamOutput, SCStreamDelegate>
- (instancetype)initWithOutputPath:(NSString *)outputPath metaPath:(NSString *)metaPath sampleRate:(double)sampleRate;
- (BOOL)startWithError:(NSError **)error;
- (void)stop;
- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer ofType:(SCStreamOutputType)type;
- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error;
@end

@implementation ScreenCaptureKitAudioCapture {
    NSString *_outputPath;
    NSString *_metaPath;
    double _requestedSampleRate;
    int _fd;
    SCStream *_stream;
    dispatch_queue_t _sampleQueue;
    id<NSObject> _displaySleepActivity;
    atomic_uint_fast64_t _bytesWritten;
    NSInteger _sampleRate;
    NSInteger _sourceChannels;
    atomic_bool _stopped;
    atomic_bool _loggedUnsupportedFormat;
    atomic_bool _streamFailureHandled;
}

- (instancetype)initWithOutputPath:(NSString *)outputPath metaPath:(NSString *)metaPath sampleRate:(double)sampleRate {
    self = [super init];
    if (self) {
        _outputPath = [outputPath copy];
        _metaPath = [metaPath copy];
        _requestedSampleRate = sampleRate > 0 ? sampleRate : 48000.0;
        _fd = -1;
        _stream = nil;
        _sampleQueue = nil;
        _displaySleepActivity = nil;
        atomic_init(&_bytesWritten, 0);
        _sampleRate = (NSInteger)MAX(8000.0, MIN(192000.0, _requestedSampleRate));
        _sourceChannels = 2;
        atomic_init(&_stopped, false);
        atomic_init(&_loggedUnsupportedFormat, false);
        atomic_init(&_streamFailureHandled, false);
    }
    return self;
}

- (void)dealloc {
    [self stop];
}

- (BOOL)startWithError:(NSError **)error {
    if (@available(macOS 13.0, *)) {
        _fd = open([_outputPath fileSystemRepresentation], O_CREAT | O_WRONLY | O_TRUNC, 0600);
        if (_fd < 0) {
            if (error != NULL) {
                *error = MakeError([NSString stringWithFormat:@"Failed to open output file: %@", _outputPath]);
            }
            return NO;
        }

        _displaySleepActivity = [[NSProcessInfo processInfo]
            beginActivityWithOptions:NSActivityIdleDisplaySleepDisabled
                              reason:@"Seam is recording system audio"];
        if (_displaySleepActivity == nil) {
            if (error != NULL) {
                *error = MakeError(@"Failed to prevent display idle sleep while recording system audio");
            }
            [self stop];
            return NO;
        }
        fprintf(stderr, "SCREEN_CAPTURE_KIT_DISPLAY_SLEEP_PREVENTION_STARTED\n");

        __block SCShareableContent *shareableContent = nil;
        __block NSError *shareableError = nil;
        dispatch_semaphore_t contentSemaphore = dispatch_semaphore_create(0);
        [SCShareableContent getShareableContentExcludingDesktopWindows:YES
                                                    onScreenWindowsOnly:YES
                                                      completionHandler:^(SCShareableContent *content, NSError *contentError) {
            shareableContent = content;
            shareableError = contentError;
            dispatch_semaphore_signal(contentSemaphore);
        }];
        if (dispatch_semaphore_wait(contentSemaphore, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC)) != 0) {
            if (error != NULL) {
                *error = MakeError(@"Timed out while loading ScreenCaptureKit shareable content");
            }
            [self stop];
            return NO;
        }
        if (shareableError != nil) {
            if (error != NULL) {
                *error = shareableError;
            }
            [self stop];
            return NO;
        }

        SCDisplay *display = shareableContent.displays.firstObject;
        if (display == nil) {
            if (error != NULL) {
                *error = MakeError(@"No display is available for ScreenCaptureKit audio capture");
            }
            [self stop];
            return NO;
        }

        SCContentFilter *filter = [[SCContentFilter alloc] initWithDisplay:display excludingWindows:@[]];
        SCStreamConfiguration *configuration = [[SCStreamConfiguration alloc] init];
        configuration.width = MAX(2, MIN((NSInteger)display.width, 16));
        configuration.height = MAX(2, MIN((NSInteger)display.height, 16));
        configuration.minimumFrameInterval = CMTimeMake(1, 1);
        configuration.queueDepth = 3;
        configuration.showsCursor = NO;
        configuration.capturesAudio = YES;
        configuration.sampleRate = _sampleRate;
        configuration.channelCount = _sourceChannels;
        configuration.excludesCurrentProcessAudio = YES;

        _stream = [[SCStream alloc] initWithFilter:filter configuration:configuration delegate:self];
        _sampleQueue = dispatch_queue_create("seam.screencapturekit.audio", DISPATCH_QUEUE_SERIAL);

        NSError *addOutputError = nil;
        if (![_stream addStreamOutput:self type:SCStreamOutputTypeAudio sampleHandlerQueue:_sampleQueue error:&addOutputError]) {
            if (error != NULL) {
                *error = addOutputError ?: MakeError(@"Failed to add ScreenCaptureKit audio output");
            }
            [self stop];
            return NO;
        }

        __block NSError *startError = nil;
        dispatch_semaphore_t startSemaphore = dispatch_semaphore_create(0);
        [_stream startCaptureWithCompletionHandler:^(NSError *captureError) {
            startError = captureError;
            dispatch_semaphore_signal(startSemaphore);
        }];
        if (dispatch_semaphore_wait(startSemaphore, dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC)) != 0) {
            if (error != NULL) {
                *error = MakeError(@"Timed out while starting ScreenCaptureKit audio capture");
            }
            [self stop];
            return NO;
        }
        if (startError != nil) {
            if (error != NULL) {
                *error = startError;
            }
            [self stop];
            return NO;
        }

        NSDictionary *metadata = @{
            @"backend": @"screencapturekit",
            @"format": @"f32le",
            @"channels": @1,
            @"source_channels": @(_sourceChannels),
            @"sample_rate": @(_sampleRate),
            @"excludes_current_process_audio": @YES,
            @"started_at": [[NSISO8601DateFormatter new] stringFromDate:[NSDate date]],
        };
        NSData *metadataData = [NSJSONSerialization dataWithJSONObject:metadata options:(NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys) error:error];
        if (metadataData == nil || ![metadataData writeToFile:_metaPath options:NSDataWritingAtomic error:error]) {
            [self stop];
            return NO;
        }

        fprintf(stderr, "SCREEN_CAPTURE_KIT_AUDIO_STARTED sample_rate=%ld format=f32le output=%s\n",
                (long)_sampleRate,
                [_outputPath fileSystemRepresentation]);
        return YES;
    }

    if (error != NULL) {
        *error = MakeError(@"ScreenCaptureKit audio capture requires macOS 13.0+");
    }
    return NO;
}

- (void)stop {
    bool wasStopped = atomic_exchange(&_stopped, true);
    if (wasStopped) {
        return;
    }

    if (_stream != nil) {
        if (@available(macOS 13.0, *)) {
            dispatch_semaphore_t stopSemaphore = dispatch_semaphore_create(0);
            [_stream stopCaptureWithCompletionHandler:^(NSError *error) {
                if (error != nil) {
                    fprintf(stderr, "SCREEN_CAPTURE_KIT_AUDIO_ERROR stop failed: %s\n", error.localizedDescription.UTF8String);
                }
                dispatch_semaphore_signal(stopSemaphore);
            }];
            dispatch_semaphore_wait(stopSemaphore, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC));
            NSError *removeError = nil;
            [_stream removeStreamOutput:self type:SCStreamOutputTypeAudio error:&removeError];
            if (removeError != nil) {
                fprintf(stderr, "SCREEN_CAPTURE_KIT_AUDIO_ERROR remove output failed: %s\n", removeError.localizedDescription.UTF8String);
            }
        }
        _stream = nil;
    }
    if (_fd >= 0) {
        close(_fd);
        _fd = -1;
    }
    if (_displaySleepActivity != nil) {
        [[NSProcessInfo processInfo] endActivity:_displaySleepActivity];
        _displaySleepActivity = nil;
        fprintf(stderr, "SCREEN_CAPTURE_KIT_DISPLAY_SLEEP_PREVENTION_STOPPED\n");
    }

    uint_fast64_t bytesWritten = atomic_load(&_bytesWritten);
    double duration = _sampleRate > 0 ? (double)bytesWritten / ((double)_sampleRate * sizeof(float)) : 0;
    fprintf(stderr, "SCREEN_CAPTURE_KIT_AUDIO_STOPPED bytes=%llu duration=%.1fs\n", (unsigned long long)bytesWritten, duration);
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    (void)stream;
    if (atomic_load(&_stopped) || atomic_exchange(&_streamFailureHandled, true)) {
        return;
    }

    NSString *domain = SingleLine(error.domain);
    NSString *description = SingleLine(error.localizedDescription);
    NSString *userInfo = SingleLine(error.userInfo.description);
    uint_fast64_t bytesWritten = atomic_load(&_bytesWritten);
    double duration = _sampleRate > 0 ? (double)bytesWritten / ((double)_sampleRate * sizeof(float)) : 0;
    fprintf(stderr,
            "SCREEN_CAPTURE_KIT_AUDIO_ERROR stream stopped domain=%s code=%ld description=%s user_info=%s bytes=%llu duration=%.3fs\n",
            domain.UTF8String,
            (long)error.code,
            description.UTF8String,
            userInfo.UTF8String,
            (unsigned long long)bytesWritten,
            duration);
    fflush(stderr);

    // A stopped SCStream cannot resume itself. Exit so the parent can replace the full session.
    dispatch_async(dispatch_get_main_queue(), ^{
        _Exit(75);
    });
}

- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sampleBuffer ofType:(SCStreamOutputType)type {
    (void)stream;
    if (type != SCStreamOutputTypeAudio || sampleBuffer == NULL || atomic_load(&_stopped) || _fd < 0) {
        return;
    }
    if (!CMSampleBufferDataIsReady(sampleBuffer)) {
        return;
    }

    CMFormatDescriptionRef formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer);
    if (formatDescription == NULL) {
        return;
    }
    const AudioStreamBasicDescription *format = CMAudioFormatDescriptionGetStreamBasicDescription(formatDescription);
    if (format == NULL) {
        return;
    }

    size_t bufferListSize = 0;
    OSStatus status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        &bufferListSize,
        NULL,
        0,
        NULL,
        NULL,
        kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
        NULL
    );
    if (status != noErr || bufferListSize == 0) {
        return;
    }

    AudioBufferList *bufferList = (AudioBufferList *)malloc(bufferListSize);
    if (bufferList == NULL) {
        return;
    }
    CMBlockBufferRef blockBuffer = NULL;
    status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
        sampleBuffer,
        NULL,
        bufferList,
        bufferListSize,
        kCFAllocatorDefault,
        kCFAllocatorDefault,
        kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
        &blockBuffer
    );
    if (status == noErr) {
        [self writeMonoFromBufferList:bufferList format:*format];
    }
    if (blockBuffer != NULL) {
        CFRelease(blockBuffer);
    }
    free(bufferList);
}

- (BOOL)supportsFormat:(AudioStreamBasicDescription)format {
    if (format.mFormatID != kAudioFormatLinearPCM) {
        return NO;
    }
    BOOL isFloat32 = (format.mFormatFlags & kAudioFormatFlagIsFloat) != 0 && format.mBitsPerChannel == 32;
    BOOL isInt16 = (format.mFormatFlags & kAudioFormatFlagIsSignedInteger) != 0 && format.mBitsPerChannel == 16;
    return isFloat32 || isInt16;
}

- (float)floatSampleAt:(const void *)base index:(UInt32)index format:(AudioStreamBasicDescription)format {
    if ((format.mFormatFlags & kAudioFormatFlagIsFloat) != 0 && format.mBitsPerChannel == 32) {
        const float *samples = (const float *)base;
        return samples[index];
    }
    if ((format.mFormatFlags & kAudioFormatFlagIsSignedInteger) != 0 && format.mBitsPerChannel == 16) {
        const int16_t *samples = (const int16_t *)base;
        return (float)samples[index] / 32768.0f;
    }
    return 0.0f;
}

- (void)writeMonoFromBufferList:(const AudioBufferList *)bufferList format:(AudioStreamBasicDescription)format {
    if (bufferList == NULL || bufferList->mNumberBuffers == 0 || ![self supportsFormat:format]) {
        bool alreadyLogged = atomic_exchange(&_loggedUnsupportedFormat, true);
        if (!alreadyLogged) {
            fprintf(stderr, "SCREEN_CAPTURE_KIT_AUDIO_ERROR unsupported format: %s\n", DescribeFormat(format).UTF8String);
        }
        return;
    }

    UInt32 bytesPerSample = MAX(format.mBitsPerChannel / 8, 1);
    BOOL nonInterleaved = (format.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0;

    if (!nonInterleaved && bufferList->mNumberBuffers == 1) {
        AudioBuffer buffer = bufferList->mBuffers[0];
        if (buffer.mData == NULL || buffer.mDataByteSize == 0) {
            return;
        }
        UInt32 channels = MAX(buffer.mNumberChannels, 1);
        UInt32 frames = buffer.mDataByteSize / (bytesPerSample * channels);
        if (frames == 0) {
            return;
        }
        NSMutableData *monoData = [NSMutableData dataWithLength:frames * sizeof(float)];
        float *mono = (float *)monoData.mutableBytes;
        for (UInt32 frame = 0; frame < frames; frame++) {
            float sum = 0.0f;
            UInt32 baseIndex = frame * channels;
            for (UInt32 ch = 0; ch < channels; ch++) {
                sum += [self floatSampleAt:buffer.mData index:(baseIndex + ch) format:format];
            }
            mono[frame] = sum / (float)channels;
        }
        [self writeBytes:monoData.bytes byteCount:(UInt32)monoData.length];
        return;
    }

    UInt32 frames = UINT32_MAX;
    for (UInt32 i = 0; i < bufferList->mNumberBuffers; i++) {
        AudioBuffer buffer = bufferList->mBuffers[i];
        if (buffer.mData == NULL || buffer.mDataByteSize == 0) {
            continue;
        }
        UInt32 channels = nonInterleaved ? 1 : MAX(buffer.mNumberChannels, 1);
        UInt32 candidateFrames = buffer.mDataByteSize / (bytesPerSample * channels);
        frames = MIN(frames, candidateFrames);
    }
    if (frames == UINT32_MAX || frames == 0) {
        return;
    }

    NSMutableData *monoData = [NSMutableData dataWithLength:frames * sizeof(float)];
    float *mono = (float *)monoData.mutableBytes;
    UInt32 usedChannels = 0;
    for (UInt32 i = 0; i < bufferList->mNumberBuffers; i++) {
        AudioBuffer buffer = bufferList->mBuffers[i];
        if (buffer.mData == NULL || buffer.mDataByteSize == 0) {
            continue;
        }
        UInt32 channels = nonInterleaved ? 1 : MAX(buffer.mNumberChannels, 1);
        for (UInt32 frame = 0; frame < frames; frame++) {
            for (UInt32 ch = 0; ch < channels; ch++) {
                UInt32 index = nonInterleaved ? frame : (frame * channels + ch);
                mono[frame] += [self floatSampleAt:buffer.mData index:index format:format];
                usedChannels++;
            }
        }
    }
    if (usedChannels == 0) {
        return;
    }
    UInt32 divisor = MAX(1, usedChannels / frames);
    for (UInt32 frame = 0; frame < frames; frame++) {
        mono[frame] /= (float)divisor;
    }
    [self writeBytes:monoData.bytes byteCount:(UInt32)monoData.length];
}

- (void)writeBytes:(const void *)bytes byteCount:(UInt32)byteCount {
    if (bytes == NULL || byteCount == 0 || atomic_load(&_stopped) || _fd < 0) {
        return;
    }
    const UInt8 *cursor = (const UInt8 *)bytes;
    UInt32 remaining = byteCount;
    while (remaining > 0) {
        ssize_t written = write(_fd, cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return;
        }
        if (written == 0) {
            return;
        }
        cursor += written;
        remaining -= (UInt32)written;
        atomic_fetch_add(&_bytesWritten, (uint_fast64_t)written);
    }
}

@end

@interface CoreAudioMicCapture : NSObject <SeamAudioCapture>
- (instancetype)initWithOutputPath:(NSString *)outputPath
                          metaPath:(NSString *)metaPath
                        sampleRate:(double)sampleRate
                        deviceName:(NSString *)deviceName
                         deviceUID:(NSString *)deviceUID
                          bufferMS:(UInt32)bufferMS;
- (BOOL)startWithError:(NSError **)error;
- (void)stop;
- (void)handleInputBuffer:(AudioQueueBufferRef)buffer queue:(AudioQueueRef)queue;
@end

static void MicrophoneInputCallback(
    void *inUserData,
    AudioQueueRef inAQ,
    AudioQueueBufferRef inBuffer,
    const AudioTimeStamp *inStartTime,
    UInt32 inNumberPacketDescriptions,
    const AudioStreamPacketDescription *inPacketDescs
) {
    (void)inStartTime;
    (void)inNumberPacketDescriptions;
    (void)inPacketDescs;
    CoreAudioMicCapture *capture = (__bridge CoreAudioMicCapture *)inUserData;
    [capture handleInputBuffer:inBuffer queue:inAQ];
}

@implementation CoreAudioMicCapture {
    NSString *_outputPath;
    NSString *_metaPath;
    NSString *_deviceName;
    NSString *_deviceUID;
    NSDictionary *_selectedDevice;
    double _requestedSampleRate;
    UInt32 _bufferMS;
    int _fd;
    AudioQueueRef _queue;
    AudioQueueBufferRef _buffers[3];
    AudioStreamBasicDescription _format;
    UInt64 _bytesWritten;
    atomic_bool _stopped;
}

- (instancetype)initWithOutputPath:(NSString *)outputPath
                          metaPath:(NSString *)metaPath
                        sampleRate:(double)sampleRate
                        deviceName:(NSString *)deviceName
                         deviceUID:(NSString *)deviceUID
                          bufferMS:(UInt32)bufferMS {
    self = [super init];
    if (self) {
        _outputPath = [outputPath copy];
        _metaPath = [metaPath copy];
        _deviceName = [deviceName copy];
        _deviceUID = [deviceUID copy];
        _requestedSampleRate = sampleRate > 0 ? sampleRate : 16000.0;
        _bufferMS = MAX(20, MIN(bufferMS, 500));
        _fd = -1;
        _queue = NULL;
        memset(_buffers, 0, sizeof(_buffers));
        memset(&_format, 0, sizeof(_format));
        _bytesWritten = 0;
        atomic_init(&_stopped, false);
    }
    return self;
}

- (void)dealloc {
    [self stop];
}

- (BOOL)startWithError:(NSError **)error {
    _fd = open([_outputPath fileSystemRepresentation], O_CREAT | O_WRONLY | O_TRUNC, 0600);
    if (_fd < 0) {
        if (error != NULL) {
            *error = MakeError([NSString stringWithFormat:@"Failed to open output file: %@", _outputPath]);
        }
        return NO;
    }

    _selectedDevice = FindInputDeviceInfo(_deviceName, _deviceUID);
    if (_selectedDevice == nil) {
        if (error != NULL) {
            *error = MakeError(@"No Core Audio input device is available");
        }
        [self stop];
        return NO;
    }

    memset(&_format, 0, sizeof(_format));
    _format.mSampleRate = _requestedSampleRate;
    _format.mFormatID = kAudioFormatLinearPCM;
    _format.mFormatFlags = kAudioFormatFlagIsFloat | kAudioFormatFlagIsPacked;
    _format.mBytesPerPacket = sizeof(float);
    _format.mFramesPerPacket = 1;
    _format.mBytesPerFrame = sizeof(float);
    _format.mChannelsPerFrame = 1;
    _format.mBitsPerChannel = 32;

    if (!CheckStatus(
            AudioQueueNewInput(&_format, MicrophoneInputCallback, (__bridge void *)self, NULL, NULL, 0, &_queue),
            @"AudioQueueNewInput",
            error
        )) {
        [self stop];
        return NO;
    }

    NSString *uid = _selectedDevice[@"uid"];
    if (uid.length > 0) {
        CFStringRef currentDevice = (__bridge CFStringRef)uid;
        if (!CheckStatus(
                AudioQueueSetProperty(_queue, kAudioQueueProperty_CurrentDevice, &currentDevice, sizeof(currentDevice)),
                @"AudioQueueSetProperty(kAudioQueueProperty_CurrentDevice)",
                error
            )) {
            [self stop];
            return NO;
        }
    }

    UInt32 formatSize = sizeof(_format);
    if (!CheckStatus(
            AudioQueueGetProperty(_queue, kAudioQueueProperty_StreamDescription, &_format, &formatSize),
            @"AudioQueueGetProperty(kAudioQueueProperty_StreamDescription)",
            error
        )) {
        [self stop];
        return NO;
    }

    BOOL isLinearPCM = _format.mFormatID == kAudioFormatLinearPCM;
    BOOL isFloat = (_format.mFormatFlags & kAudioFormatFlagIsFloat) != 0;
    if (!isLinearPCM || !isFloat || _format.mBitsPerChannel != 32 || _format.mChannelsPerFrame != 1) {
        if (error != NULL) {
            *error = MakeError([NSString stringWithFormat:@"Unsupported microphone format: %@", DescribeFormat(_format)]);
        }
        [self stop];
        return NO;
    }

    NSDictionary *metadata = @{
        @"backend": @"coreaudio_mic",
        @"format": @"f32le",
        @"channels": @1,
        @"sample_rate": @(_format.mSampleRate),
        @"device_name": _selectedDevice[@"name"] ?: @"",
        @"device_uid": _selectedDevice[@"uid"] ?: @"",
        @"requested_device_name": _deviceName ?: @"",
        @"buffer_ms": @(_bufferMS),
        @"started_at": [[NSISO8601DateFormatter new] stringFromDate:[NSDate date]],
    };
    NSData *metadataData = [NSJSONSerialization dataWithJSONObject:metadata options:(NSJSONWritingPrettyPrinted | NSJSONWritingSortedKeys) error:error];
    if (metadataData == nil || ![metadataData writeToFile:_metaPath options:NSDataWritingAtomic error:error]) {
        [self stop];
        return NO;
    }

    UInt32 framesPerBuffer = (UInt32)MAX(256.0, (_format.mSampleRate * (double)_bufferMS) / 1000.0);
    UInt32 bytesPerBuffer = framesPerBuffer * _format.mBytesPerFrame;
    for (NSUInteger i = 0; i < sizeof(_buffers) / sizeof(_buffers[0]); i++) {
        if (!CheckStatus(AudioQueueAllocateBuffer(_queue, bytesPerBuffer, &_buffers[i]), @"AudioQueueAllocateBuffer", error)) {
            [self stop];
            return NO;
        }
        if (!CheckStatus(AudioQueueEnqueueBuffer(_queue, _buffers[i], 0, NULL), @"AudioQueueEnqueueBuffer", error)) {
            [self stop];
            return NO;
        }
    }

    if (!CheckStatus(AudioQueueStart(_queue, NULL), @"AudioQueueStart", error)) {
        [self stop];
        return NO;
    }

    fprintf(stderr, "CORE_AUDIO_MIC_STARTED sample_rate=%.0f format=f32le device=\"%s\" output=%s\n",
            _format.mSampleRate,
            [_selectedDevice[@"name"] UTF8String],
            [_outputPath fileSystemRepresentation]);
    return YES;
}

- (void)stop {
    bool wasStopped = atomic_exchange(&_stopped, true);
    if (wasStopped) {
        return;
    }

    if (_queue != NULL) {
        AudioQueueStop(_queue, true);
        AudioQueueDispose(_queue, true);
        _queue = NULL;
        memset(_buffers, 0, sizeof(_buffers));
    }
    if (_fd >= 0) {
        close(_fd);
        _fd = -1;
    }

    double duration = _format.mSampleRate > 0 ? (double)_bytesWritten / (_format.mSampleRate * sizeof(float)) : 0;
    fprintf(stderr, "CORE_AUDIO_MIC_STOPPED bytes=%llu duration=%.1fs\n", _bytesWritten, duration);
}

- (void)handleInputBuffer:(AudioQueueBufferRef)buffer queue:(AudioQueueRef)queue {
    if (buffer != NULL && buffer->mAudioData != NULL && buffer->mAudioDataByteSize > 0 && !atomic_load(&_stopped)) {
        [self writeBytes:buffer->mAudioData byteCount:buffer->mAudioDataByteSize];
    }
    if (!atomic_load(&_stopped) && queue != NULL && buffer != NULL) {
        AudioQueueEnqueueBuffer(queue, buffer, 0, NULL);
    }
}

- (void)writeBytes:(const void *)bytes byteCount:(UInt32)byteCount {
    if (bytes == NULL || byteCount == 0 || atomic_load(&_stopped) || _fd < 0) {
        return;
    }
    const UInt8 *cursor = (const UInt8 *)bytes;
    UInt32 remaining = byteCount;
    while (remaining > 0) {
        ssize_t written = write(_fd, cursor, remaining);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return;
        }
        if (written == 0) {
            return;
        }
        cursor += written;
        remaining -= (UInt32)written;
        _bytesWritten += (UInt64)written;
    }
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            fprintf(stderr, "Usage: audio-capture <output.raw> [--mode system|screencapturekit|microphone] [--meta-path capture.meta.json] [--sample-rate 16000] [--mic-device-name name]\n");
            return 2;
        }

        NSString *outputPath = [NSString stringWithUTF8String:argv[1]];
        NSString *metaPath = [[outputPath stringByDeletingPathExtension] stringByAppendingPathExtension:@"meta.json"];
        NSString *mode = @"system";
        NSString *micDeviceName = nil;
        NSString *micDeviceUID = nil;
        double sampleRate = 16000.0;
        UInt32 bufferMS = 100;
        for (int i = 2; i < argc; i++) {
            if (strcmp(argv[i], "--meta-path") == 0 && i + 1 < argc) {
                metaPath = [NSString stringWithUTF8String:argv[i + 1]];
                i++;
            } else if (strcmp(argv[i], "--mode") == 0 && i + 1 < argc) {
                mode = [[NSString stringWithUTF8String:argv[i + 1]] lowercaseString];
                i++;
            } else if (strcmp(argv[i], "--sample-rate") == 0 && i + 1 < argc) {
                sampleRate = atof(argv[i + 1]);
                if (sampleRate < 8000.0 || sampleRate > 192000.0) {
                    sampleRate = 16000.0;
                }
                i++;
            } else if ((strcmp(argv[i], "--mic-device-name") == 0 || strcmp(argv[i], "--device-name") == 0) && i + 1 < argc) {
                micDeviceName = [NSString stringWithUTF8String:argv[i + 1]];
                i++;
            } else if ((strcmp(argv[i], "--mic-device-uid") == 0 || strcmp(argv[i], "--device-uid") == 0) && i + 1 < argc) {
                micDeviceUID = [NSString stringWithUTF8String:argv[i + 1]];
                i++;
            } else if (strcmp(argv[i], "--buffer-ms") == 0 && i + 1 < argc) {
                bufferMS = (UInt32)MAX(20, MIN(500, atoi(argv[i + 1])));
                i++;
            }
        }

        id<SeamAudioCapture> capture = nil;
        NSString *errorPrefix = @"SCREEN_CAPTURE_KIT_AUDIO_ERROR";
        if ([mode isEqualToString:@"mic"] || [mode isEqualToString:@"microphone"]) {
            capture = [[CoreAudioMicCapture alloc] initWithOutputPath:outputPath
                                                              metaPath:metaPath
                                                            sampleRate:sampleRate
                                                            deviceName:micDeviceName
                                                             deviceUID:micDeviceUID
                                                              bufferMS:bufferMS];
            errorPrefix = @"CORE_AUDIO_MIC_ERROR";
        } else if ([mode isEqualToString:@"system"] ||
                   [mode isEqualToString:@"sck"] ||
                   [mode isEqualToString:@"screen_capture_kit"] ||
                   [mode isEqualToString:@"screen-capture-kit"] ||
                   [mode isEqualToString:@"screen_capturekit"] ||
                   [mode isEqualToString:@"screencapturekit"]) {
            capture = [[ScreenCaptureKitAudioCapture alloc] initWithOutputPath:outputPath
                                                                      metaPath:metaPath
                                                                    sampleRate:sampleRate];
        } else {
            fprintf(stderr, "AUDIO_CAPTURE_ERROR unsupported mode: %s\n", mode.UTF8String);
            return 2;
        }

        NSError *error = nil;
        if (![capture startWithError:&error]) {
            fprintf(stderr,
                    "%s start failed domain=%s code=%ld description=%s user_info=%s\n",
                    errorPrefix.UTF8String,
                    SingleLine(error.domain).UTF8String,
                    (long)error.code,
                    SingleLine(error.localizedDescription).UTF8String,
                    SingleLine(error.userInfo.description).UTF8String);
            return 1;
        }

        void (^stopAndExit)(void) = ^{
            [capture stop];
            exit(0);
        };

        dispatch_source_t sigint = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGINT, 0, dispatch_get_main_queue());
        signal(SIGINT, SIG_IGN);
        dispatch_source_set_event_handler(sigint, stopAndExit);
        dispatch_resume(sigint);

        dispatch_source_t sigterm = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0, dispatch_get_main_queue());
        signal(SIGTERM, SIG_IGN);
        dispatch_source_set_event_handler(sigterm, stopAndExit);
        dispatch_resume(sigterm);

        dispatch_main();
    }
}
