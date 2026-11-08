// AGVRobot.h
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Pawn.h"
#include "AGVRobot.generated.h"

class UBoxComponent;
class UStaticMeshComponent;
class USpringArmComponent;
class UCameraComponent;
class UInputComponent;
class USphereComponent;
class USceneComponent;
class AAGVGrabbableObject;
class AAGVPlacementPreview;
class AAGVDisplayShelf;
class AWarehouseMapLoader;
class USplineComponent;
struct FWarehouseRouteStep;

// AGV 위치 제어 방식. 현재는 Manual만 실제 이동을 처리한다.
UENUM(BlueprintType)
enum class EAGVMovementMode : uint8
{
    Manual UMETA(DisplayName = "Manual"),
    Simulation UMETA(DisplayName = "Simulation"),
    External UMETA(DisplayName = "External")
};

UCLASS()
class AGVSIMULATION_API AAGVRobot : public APawn
{
    GENERATED_BODY()

#pragma region Public
public:
    AAGVRobot();

    virtual void Tick(float DeltaTime) override;

    virtual void SetupPlayerInputComponent(
        UInputComponent* PlayerInputComponent
    ) override;

    UFUNCTION(BlueprintPure, Category = "Interaction")
    bool CanGrabObject() const;

    UFUNCTION(BlueprintPure, Category = "Interaction")
    bool IsHoldingObject() const;

    // 서버가 지정한 노드 경로로 이동
    UFUNCTION(BlueprintCallable, Category = "Movement|Simulation")
    bool StartServerRoute(
        const TArray<FString>& NodeIds,
        const FString& ReleasedUntil
    );

    // 서버에서 전달한 AGV 상태 반영
    UFUNCTION(BlueprintCallable, Category = "Movement|Server")
    void UpdateServerState(
        const FString& InStatus,
        const FString& InCurrentNode,
        float InBattery
    );

    UFUNCTION(BlueprintCallable, Category = "Movement|External")
    void UpdateExternalPosition(const FString& NodeId);

    UFUNCTION(BlueprintCallable, Category = "Movement|External")
    void UpdateExternalPose(
        float X,
        float Y,
        float Yaw
    );

    void ResetExternalPoseInitialization();
#pragma endregion

#pragma region Protected

protected:
    virtual void BeginPlay() override;

    // 충돌 박스
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "AGV")
    UBoxComponent* CollisionBox;

    // Meshy AI Static Mesh
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "AGV")
    UStaticMeshComponent* RobotMesh;

    // 카메라 암
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
    USpringArmComponent* SpringArm;

    // 카메라
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Camera")
    UCameraComponent* Camera;

    // 현재 AGV 이동 모드 (기본값: 기존 수동 조작)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
    EAGVMovementMode MovementMode = EAGVMovementMode::Manual;

    // 서버가 보고한 AGV 상태
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Movement|Server")
    FString ServerStatus = TEXT("unknown");

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Movement|Server")
    FString ServerCurrentNode;

    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Movement|Server")
    float ServerBattery = 0.0f;

    // Simulation 시작/목적지 노드 ID
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|Simulation")
    FString SimulationStartNode = TEXT("J01");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|Simulation")
    FString SimulationDestinationNode = TEXT("D01");

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|Simulation")
    float SimulationSpeed = 150.0f;

    // 시뮬레이션 이동 중 충돌 시 정지
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|Simulation")
    bool bSimulationSweep = false;

    // 기존 시뮬레이션 경로 자동 시작 여부
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|Simulation")
    bool bAutoStartSimulationRoute = true;

    UFUNCTION(BlueprintCallable, Category = "Movement|Simulation")
    bool StartSimulationRoute(const FString& StartId, const FString& DestinationId);

    UFUNCTION(BlueprintCallable, Category = "Movement|Simulation")
    void StopSimulationRoute();

    // 이동 속도 (cm/s)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
    float MoveSpeed = 300.0f;

    // 회전 속도 (degree/s)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement")
    float TurnSpeed = 90.0f;

    // 물체 감지 범위
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Grab")
    USphereComponent* GrabRange;

    // 물체가 부착될 위치
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Grab")
    USceneComponent* GrabPoint;

    // 집기 가능 거리 (cm)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Grab")
    float GrabDistance = 200.0f;

    // 배치 미리보기 블루프린트 클래스
    UPROPERTY(EditDefaultsOnly, Category = "Placement")
    TSubclassOf<AAGVPlacementPreview> PlacementPreviewClass;

    // 현재 생성된 미리보기 액터
    UPROPERTY()
    TObjectPtr<AAGVPlacementPreview> PlacementPreview;

    // 현재 조준 중인 진열대와 슬롯
    UPROPERTY()
    TObjectPtr<AAGVDisplayShelf> AimedShelf;

    int32 AimedSlotIndex = INDEX_NONE;

    // 미리보기 갱신
    void UpdatePlacementPreview();

    // 선택된 주행 경로 강조 표시
    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "Movement|Simulation|Highlight")
    bool bShowPlannedRoute = true;

    // 하늘색 강조선
    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "Movement|Simulation|Highlight")
    FLinearColor PlannedRouteColor =
        FLinearColor(0.0f, 0.9f, 1.0f, 1.0f);

    // 강조선 두께
    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "Movement|Simulation|Highlight")
    float PlannedRouteThickness = 8.0f;

    // 기존 경로보다 위에 표시할 높이 (cm)
    UPROPERTY(EditAnywhere, BlueprintReadWrite,
        Category = "Movement|Simulation|Highlight")
    float PlannedRouteZOffset = 10.0f;

    // 경로 강조 표시 함수
    void DrawPlannedRoute() const;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalPositionScale = 100.0f;

    // External 위치 보간 설정
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalInterpolationDuration = 1.0f;

    // 보간 시작 위치 및 목표 위치
    FVector ExternalStartPosition = FVector::ZeroVector;
    FVector ExternalTargetPosition = FVector::ZeroVector;

    FRotator ExternalStartRotation = FRotator::ZeroRotator;
    FRotator ExternalTargetRotation = FRotator::ZeroRotator;

    float ExternalInterpolationElapsed = 0.0f;
    bool bExternalInterpolating = false;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalRotationDuration = 0.5f;

    // 동일한 위치로 판단할 거리 오차 (cm)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalPositionTolerance = 1.0f;

    // 동일한 방향으로 판단할 각도 오차 (degree)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalRotationTolerance = 1.0f;

    // External 모드에서 AGV의 최대 시각화 이동 속도 (cm/s)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalMoveSpeed = 300.0f;

    // 한 번의 보간에 사용할 최대 시간 (초)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalMaxInterpolationDuration = 0.8f;

    // 한 번의 보간에 사용할 최소 시간 (초)
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalMinInterpolationDuration = 0.05f;

    // External 모드에서 데이터 수신 중단으로 판단할 시간
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Movement|External")
    float ExternalDataTimeout = 3.0f;
#pragma endregion

#pragma region Private
private:
    float MoveInput = 0.0f;
    float TurnInput = 0.0f;

    UPROPERTY(Transient)
    TObjectPtr<AWarehouseMapLoader> MapLoader;

    UPROPERTY(Transient)
    TObjectPtr<USplineComponent> SimulationSpline;

    TArray<int32> RouteEdgeIndices;
    TArray<bool> RouteReversed;
    int32 CurrentRouteStep = 0;
    float CurrentEdgeDistance = 0.0f;
    float SimulationHeight = 0.0f;
    bool bRouteActive = false;
    bool bAutoRouteAttempted = false;
    // 서버 경로 수신 후 기존 자동 경로 시작 방지
    bool bServerRouteReceived = false;

    // 서버가 전달한 전체 경로
    TArray<FString> ServerRouteNodes;

    // 현재 서버가 주행을 허가한 마지막 노드의 인덱스
    int32 ServerReleasedNodeIndex = INDEX_NONE;

    // AGV가 실제로 도착한 마지막 서버 경로 노드 인덱스
    int32 ServerReachedNodeIndex = INDEX_NONE;

    void UpdateSimulationMovement(float DeltaTime);
    bool PrepareSimulationEdge();

    // 기존 수동 이동 코드를 별도 함수로 분리
    void UpdateManualMovement(float DeltaTime);

    void MoveForward(float Value);
    void Turn(float Value);

    void LookRight(float Value);
    void LookUp(float Value);

    void UpdateGrabTarget();

    void GrabObject();
    void ReleaseObject();

    UPROPERTY()
    AAGVGrabbableObject* HeldActor = nullptr;

    UPROPERTY()
    AAGVGrabbableObject* TargetActor = nullptr;

    void UpdateExternalInterpolation(float DeltaTime);

    bool bExternalPoseInitialized = false;

    // External 회전 단계
    bool bExternalRotating = false;

    // 제자리 회전에 사용할 시간
    float ExternalRotationElapsed = 0.0f;

    // 현재 목표까지 계산된 이동 시간
    float ExternalCurrentMoveDuration = 0.8f;

    // 마지막 External 좌표 수신 이후 경과 시간
    float ExternalDataElapsed = 0.0f;

    // External 데이터 수신 여부
    bool bExternalDataReceived = false;

    // 데이터 수신 중단 상태
    bool bExternalDataStale = false;
#pragma endregion
};
