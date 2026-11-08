#include "AGVWebSocketClient.h"
#include "WebSocketsModule.h"
#include "Modules/ModuleManager.h"
#include "AGVRobot.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonReader.h"
#include "EngineUtils.h"
#include "TimerManager.h"
#include "Engine/World.h"

AAGVWebSocketClient::AAGVWebSocketClient()
{
    PrimaryActorTick.bCanEverTick = false;
}

void AAGVWebSocketClient::BeginPlay()
{
    Super::BeginPlay();

    if (bConnectOnBeginPlay)
    {
        ConnectToServer();
    }
}

void AAGVWebSocketClient::EndPlay(
    const EEndPlayReason::Type EndPlayReason)
{
    bShuttingDown = true;

    if (GetWorld())
    {
        GetWorld()->GetTimerManager().ClearTimer(
            ReconnectTimerHandle);
    }

    DisconnectFromServer();

    Super::EndPlay(EndPlayReason);
}

void AAGVWebSocketClient::ConnectToServer()
{
    if (bShuttingDown)
    {
        return;
    }

    bManualDisconnect = false;

    if (GetWorld())
    {
        GetWorld()->GetTimerManager().ClearTimer(
            ReconnectTimerHandle);
    }

    // 기존 연결 정리
    if (Socket.IsValid())
    {
        Socket->OnConnected().Clear();
        Socket->OnConnectionError().Clear();
        Socket->OnClosed().Clear();
        Socket->OnMessage().Clear();

        Socket->Close();
        Socket.Reset();
    }

    FWebSocketsModule& WebSocketsModule =
        FModuleManager::LoadModuleChecked<FWebSocketsModule>(
            TEXT("WebSockets"));

    Socket = WebSocketsModule.CreateWebSocket(ServerURL);

    Socket->OnConnected().AddLambda([this]()
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[WebSocket] Connected to server"));

            if (GetWorld())
            {
                GetWorld()->GetTimerManager().ClearTimer(
                    ReconnectTimerHandle);

                for (TActorIterator<AAGVRobot> It(GetWorld()); It; ++It)
                {
                    AAGVRobot* Robot = *It;

                    if (IsValid(Robot))
                    {
                        Robot->ResetExternalPoseInitialization();
                        break;
                    }
                }
            }
        });

    Socket->OnConnectionError().AddLambda(
        [this](const FString& Error)
        {
            UE_LOG(LogTemp, Error,
                TEXT("[WebSocket] Connection error: %s"),
                *Error);

            ScheduleReconnect();
        });

    Socket->OnClosed().AddLambda(
        [this](int32 StatusCode,
            const FString& Reason,
            bool bWasClean)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[WebSocket] Closed: %d / %s"),
                StatusCode, *Reason);

            ScheduleReconnect();
        });

    Socket->OnMessage().AddLambda(
        [this](const FString& Message)
        {
            HandleMessage(Message);
        });

    UE_LOG(LogTemp, Warning,
        TEXT("[WebSocket] Connecting to %s"),
        *ServerURL);

    Socket->Connect();
}

void AAGVWebSocketClient::ScheduleReconnect()
{
    if (!bAutoReconnect ||
        bManualDisconnect ||
        bShuttingDown ||
        !GetWorld())
    {
        return;
    }

    FTimerManager& TimerManager =
        GetWorld()->GetTimerManager();

    // 이미 재연결이 예약되어 있다면 중복 예약 방지
    if (TimerManager.IsTimerActive(ReconnectTimerHandle))
    {
        return;
    }

    UE_LOG(LogTemp, Warning,
        TEXT("[WebSocket] Reconnecting in %.1f seconds"),
        ReconnectInterval);

    TimerManager.SetTimer(
        ReconnectTimerHandle,
        this,
        &AAGVWebSocketClient::AttemptReconnect,
        FMath::Max(ReconnectInterval, 0.1f),
        false
    );
}

void AAGVWebSocketClient::AttemptReconnect()
{
    if (bManualDisconnect || bShuttingDown)
    {
        return;
    }

    if (IsConnected())
    {
        return;
    }

    UE_LOG(LogTemp, Warning,
        TEXT("[WebSocket] Attempting reconnection..."));

    ConnectToServer();
}

void AAGVWebSocketClient::DisconnectFromServer()
{
    // 수동 연결 종료 시 자동 재연결하지 않음
    bManualDisconnect = true;

    if (GetWorld())
    {
        GetWorld()->GetTimerManager().ClearTimer(
            ReconnectTimerHandle);
    }

    if (Socket.IsValid())
    {
        Socket->OnConnected().Clear();
        Socket->OnConnectionError().Clear();
        Socket->OnClosed().Clear();
        Socket->OnMessage().Clear();

        Socket->Close();
        Socket.Reset();
    }

    UE_LOG(LogTemp, Warning,
        TEXT("[WebSocket] Disconnected manually"));
}

bool AAGVWebSocketClient::IsConnected() const
{
    return Socket.IsValid() && Socket->IsConnected();
}

void AAGVWebSocketClient::HandleMessage(
    const FString& Message)
{
    UE_LOG(LogTemp, Warning,
        TEXT("[WebSocket] Received: %s"),
        *Message);

    TSharedPtr<FJsonObject> RootObject;

    TSharedRef<TJsonReader<>> Reader =
        TJsonReaderFactory<>::Create(Message);

    if (!FJsonSerializer::Deserialize(Reader, RootObject)
        || !RootObject.IsValid())
    {
        UE_LOG(LogTemp, Error,
            TEXT("[WebSocket] Invalid JSON"));
        return;
    }

    FString MessageType;

    if (!RootObject->TryGetStringField(
        TEXT("type"), MessageType))
    {
        return;
    }

    if (MessageType == TEXT("robot.state"))
    {
        const TSharedPtr<FJsonObject>* DataObject = nullptr;

        if (!RootObject->TryGetObjectField(TEXT("data"), DataObject) ||
            !DataObject || !DataObject->IsValid())
        {
            UE_LOG(LogTemp, Warning,
                TEXT("[WebSocket] robot.state missing data"));
            return;
        }

        FString AGVId;
        FString Status;
        FString CurrentNode;
        double Battery = 0.0;

        (*DataObject)->TryGetStringField(TEXT("agv_id"), AGVId);
        (*DataObject)->TryGetStringField(TEXT("status"), Status);
        (*DataObject)->TryGetStringField(TEXT("current_node"), CurrentNode);
        (*DataObject)->TryGetNumberField(TEXT("battery"), Battery);

        UE_LOG(LogTemp, Warning,
            TEXT("[WebSocket] State received: %s, %s, %s, %.1f"),
            *AGVId,
            *Status,
            *CurrentNode,
            Battery);

        for (TActorIterator<AAGVRobot> It(GetWorld()); It; ++It)
        {
            AAGVRobot* Robot = *It;

            if (IsValid(Robot))
            {
                Robot->UpdateServerState(
                    Status,
                    CurrentNode,
                    static_cast<float>(Battery)
                );

                // 서버에 연속 좌표가 있는지 확인
                double X = 0.0;
                double Y = 0.0;
                double Yaw = 0.0;

                const bool bHasX =
                    (*DataObject)->TryGetNumberField(TEXT("x"), X);

                const bool bHasY =
                    (*DataObject)->TryGetNumberField(TEXT("y"), Y);

                const bool bHasYaw =
                    (*DataObject)->TryGetNumberField(TEXT("yaw"), Yaw);

                if (bHasX && bHasY && bHasYaw)
                {
                    Robot->UpdateExternalPose(
                        static_cast<float>(X),
                        static_cast<float>(Y),
                        static_cast<float>(Yaw)
                    );
                }
                else if (!CurrentNode.IsEmpty())
                {
                    Robot->UpdateExternalPosition(CurrentNode);
                }

                break;
            }
        }

        return;
    }

    if (MessageType != TEXT("robot.route"))
    {
        return;
    }

    const TSharedPtr<FJsonObject>* DataObject = nullptr;

    if (!RootObject->TryGetObjectField(
        TEXT("data"), DataObject)
        || !DataObject
        || !DataObject->IsValid())
    {
        return;
    }

    FString AgvId;
    FString ReleasedUntil;

    if (!(*DataObject)->TryGetStringField(
        TEXT("agv_id"), AgvId)
        || !(*DataObject)->TryGetStringField(
            TEXT("released_until"), ReleasedUntil))
    {
        return;
    }

    const TArray<TSharedPtr<FJsonValue>>* NodesArray = nullptr;

    if (!(*DataObject)->TryGetArrayField(
        TEXT("nodes"), NodesArray))
    {
        return;
    }

    TArray<FString> NodeIds;

    for (const TSharedPtr<FJsonValue>& Value : *NodesArray)
    {
        FString NodeId;

        if (!Value.IsValid() || !Value->TryGetString(NodeId))
        {
            UE_LOG(LogTemp, Error,
                TEXT("[WebSocket] Invalid route node"));
            return;
        }

        NodeIds.Add(NodeId);
    }

    UE_LOG(LogTemp, Warning,
        TEXT("[WebSocket] Route received: %s, nodes=%d, released_until=%s"),
        *AgvId,
        NodeIds.Num(),
        *ReleasedUntil);

    // 현재는 단일 AGV 테스트
    for (TActorIterator<AAGVRobot> It(GetWorld()); It; ++It)
    {
        AAGVRobot* Robot = *It;

        if (IsValid(Robot))
        {
            Robot->StartServerRoute(
                NodeIds,
                ReleasedUntil);
            break;
        }
    }
}
