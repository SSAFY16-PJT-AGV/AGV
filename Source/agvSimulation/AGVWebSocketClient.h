#pragma once
#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "IWebSocket.h"
#include "AGVWebSocketClient.generated.h"

UCLASS()
class AGVSIMULATION_API AAGVWebSocketClient : public AActor
{
    GENERATED_BODY()

public:
    AAGVWebSocketClient();

    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "WebSocket|Reconnect")
    bool bAutoReconnect = true;

    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "WebSocket|Reconnect",
        meta = (ClampMin = "0.1"))
    float ReconnectInterval = 3.0f;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(
        const EEndPlayReason::Type EndPlayReason) override;

    // 자동 재연결 타이머
    FTimerHandle ReconnectTimerHandle;

    // 재연결 예약
    void ScheduleReconnect();

    // 타이머 실행 시 재연결
    void AttemptReconnect();
public:
    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "WebSocket")
    FString ServerURL =
        TEXT("ws://127.0.0.1:8000/ws/digital-twin");

    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "WebSocket")
    bool bConnectOnBeginPlay = true;

    UFUNCTION(BlueprintCallable, Category = "WebSocket")
    void ConnectToServer();

    UFUNCTION(BlueprintCallable, Category = "WebSocket")
    void DisconnectFromServer();

    UFUNCTION(BlueprintPure, Category = "WebSocket")
    bool IsConnected() const;

private:
    TSharedPtr<IWebSocket> Socket;

    void HandleMessage(const FString& Message);

    // 사용자가 직접 연결을 종료했는지 여부
    bool bManualDisconnect = false;

    // PIE 종료 중인지 여부
    bool bShuttingDown = false;
};
