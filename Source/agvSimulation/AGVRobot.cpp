#include "AGVRobot.h"
#include "Components/BoxComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Components/InputComponent.h"
#include "GameFramework/SpringArmComponent.h"
#include "GameFramework/PlayerController.h"
#include "Camera/CameraComponent.h"
#include "Components/SphereComponent.h"
#include "Components/SceneComponent.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/World.h"
#include "AGVGrabbableObject.h"
#include "CollisionQueryParams.h"
#include "AGVDisplayShelf.h"
#include "EngineUtils.h"
#include "AGVPlacementPreview.h"
#include "WarehouseMapLoader.h"
#include "Components/SplineComponent.h"
#include "Algo/Reverse.h"
#include "DrawDebugHelpers.h"
#include "AGVPathActor.h"

AAGVRobot::AAGVRobot()
{
    PrimaryActorTick.bCanEverTick = true;

    AutoPossessPlayer = EAutoReceiveInput::Player0;

    SimulationSpline = CreateDefaultSubobject<USplineComponent>(TEXT("SimulationSpline"));
    SimulationSpline->SetupAttachment(RootComponent);
    // 로봇이 움직여도 월드 경로 자체는 움직이지 않도록 고정
    SimulationSpline->SetAbsolute(true, true, true);
    // 컨트롤러의 좌우 회전을 AGV에 적용
    bUseControllerRotationYaw = true;
    bUseControllerRotationPitch = false;
    bUseControllerRotationRoll = false;

    // 충돌 박스
    CollisionBox = CreateDefaultSubobject<UBoxComponent>(
        TEXT("CollisionBox"));

    RootComponent = CollisionBox;

    CollisionBox->SetBoxExtent(
        FVector(70.0f, 50.0f, 30.0f));

    CollisionBox->SetCollisionProfileName(TEXT("Pawn"));

    // Meshy AI 모델
    RobotMesh = CreateDefaultSubobject<UStaticMeshComponent>(
        TEXT("RobotMesh"));

    RobotMesh->SetupAttachment(RootComponent);

    RobotMesh->SetCollisionEnabled(
        ECollisionEnabled::NoCollision);

    // 카메라 암
    SpringArm = CreateDefaultSubobject<USpringArmComponent>(
        TEXT("SpringArm"));

    SpringArm->SetupAttachment(RootComponent);
    SpringArm->TargetArmLength = 500.0f;

    // 컨트롤러의 카메라 회전 사용
    SpringArm->bUsePawnControlRotation = true;

    // 카메라
    Camera = CreateDefaultSubobject<UCameraComponent>(
        TEXT("Camera"));

    Camera->SetupAttachment(SpringArm);
    Camera->bUsePawnControlRotation = false;

    // 집기 가능 범위
    GrabRange = CreateDefaultSubobject<USphereComponent>(
        TEXT("GrabRange"));

    GrabRange->SetupAttachment(RootComponent);
    GrabRange->SetSphereRadius(GrabDistance);
    GrabRange->SetGenerateOverlapEvents(true);

    GrabRange->SetCollisionEnabled(
        ECollisionEnabled::QueryOnly);

    GrabRange->SetCollisionResponseToAllChannels(
        ECR_Ignore);

    GrabRange->SetCollisionResponseToChannel(
        ECC_WorldDynamic, ECR_Overlap);

    GrabPoint = CreateDefaultSubobject<USceneComponent>(
        TEXT("GrabPoint"));

    GrabPoint->SetupAttachment(RootComponent);

    GrabPoint->SetRelativeLocation(
        FVector(80.0f, 0.0f, 15.0f));
}

void AAGVRobot::BeginPlay()
{
    Super::BeginPlay();

    // 시작 시 AGV 뒤쪽 위에서 바라보는 카메라
    if (APlayerController* PC =
        Cast<APlayerController>(GetController()))
    {
        PC->SetControlRotation(
            FRotator(
                -35.0f,
                GetActorRotation().Yaw,
                0.0f
            )
        );
    }

    // 배치 미리보기 액터 생성
    if (PlacementPreviewClass)
    {
        PlacementPreview = GetWorld()->SpawnActor<AAGVPlacementPreview>(
            PlacementPreviewClass,
            FTransform::Identity
        );

        if (IsValid(PlacementPreview))
        {
            PlacementPreview->HidePreview();
        }
    }
}

void AAGVRobot::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);

    // 이동 방식에 따라 위치 제어 로직을 분리한다.
    switch (MovementMode)
    {
    case EAGVMovementMode::Manual:
        UpdateManualMovement(DeltaTime);
        break;

    case EAGVMovementMode::Simulation:
        UpdateSimulationMovement(DeltaTime);
        DrawPlannedRoute();
        break;

    case EAGVMovementMode::External:

        if (bExternalDataReceived)
        {
            ExternalDataElapsed += DeltaTime;

            if (!bExternalDataStale &&
                ExternalDataElapsed >= ExternalDataTimeout)
            {
                bExternalDataStale = true;

                // 오래된 목표를 향한 이동 중단
                bExternalRotating = false;
                bExternalInterpolating = false;

                UE_LOG(
                    LogTemp,
                    Warning,
                    TEXT("[External] Data timeout! No position update for %.1f seconds"),
                    ExternalDataElapsed
                );
            }
        }

        if (!bExternalDataStale)
        {
            UpdateExternalInterpolation(DeltaTime);
        }

        break;

    default:
        break;
    }

    UpdatePlacementPreview();

    UpdateGrabTarget();
}

void AAGVRobot::UpdateManualMovement(float DeltaTime)
{
    // 기존 A/D 회전 로직 유지
    if (!FMath::IsNearlyZero(TurnInput) && Controller)
    {
        FRotator ControlRotation = Controller->GetControlRotation();
        ControlRotation.Yaw += TurnInput * TurnSpeed * DeltaTime;
        Controller->SetControlRotation(ControlRotation);
    }

    // 기존 W/S 전진·후진 로직 유지
    if (!FMath::IsNearlyZero(MoveInput))
    {
        const FVector Direction = GetActorForwardVector();
        AddActorWorldOffset(Direction * MoveInput * MoveSpeed * DeltaTime, true);
    }
}

void AAGVRobot::MoveForward(float Value)
{
    MoveInput = Value;
}

void AAGVRobot::Turn(float Value)
{
    TurnInput = Value;
}

void AAGVRobot::LookRight(float Value)
{
    AddControllerYawInput(Value);
}

void AAGVRobot::LookUp(float Value)
{
    AddControllerPitchInput(Value);
}

void AAGVRobot::SetupPlayerInputComponent(
    UInputComponent* PlayerInputComponent)
{
    Super::SetupPlayerInputComponent(PlayerInputComponent);

    // 이동
    PlayerInputComponent->BindAxis(
        "MoveForward", this, &AAGVRobot::MoveForward);

    // 키보드 회전
    PlayerInputComponent->BindAxis(
        "Turn", this, &AAGVRobot::Turn);

    // 마우스 좌우 회전
    PlayerInputComponent->BindAxis(
        "LookRight", this, &AAGVRobot::LookRight);

    // 마우스 상하 회전
    PlayerInputComponent->BindAxis(
        "LookUp", this, &AAGVRobot::LookUp);

    // 마우스 좌클릭 물체 집기
    PlayerInputComponent->BindAction(
        "GrabObject",
        IE_Pressed,
        this,
        &AAGVRobot::GrabObject
    );

    // 마우스 우클릭 물체 놓기
    PlayerInputComponent->BindAction(
        "ReleaseObject",
        IE_Pressed,
        this,
        &AAGVRobot::ReleaseObject
    );

}

void AAGVRobot::UpdateGrabTarget()
{
    // 기존 강조 대상 초기화
    if (IsValid(TargetActor))
    {
        TargetActor->SetHighlighted(false);
    }

    TargetActor = nullptr;

    // 이미 물체를 들고 있다면 새 물체 선택 불가
    if (IsValid(HeldActor))
    {
        return;
    }

    if (!Camera || !GetWorld())
    {
        return;
    }

    // 카메라 위치와 바라보는 방향
    const FVector Start = Camera->GetComponentLocation();
    const FVector Direction = Camera->GetForwardVector();

    // 카메라에서 1000cm까지 탐색
    const FVector End = Start + Direction * 1000.0f;

    FHitResult Hit;

    FCollisionQueryParams Params;
    Params.AddIgnoredActor(this);

    // 카메라 방향으로 충돌 검사
    const bool bHit = GetWorld()->LineTraceSingleByChannel(
        Hit,
        Start,
        End,
        ECC_Visibility,
        Params
    );

    if (!bHit)
    {
        return;
    }

    AAGVGrabbableObject* HitObject =
        Cast<AAGVGrabbableObject>(Hit.GetActor());

    if (!IsValid(HitObject))
    {
        return;
    }

    // AGV에서 너무 먼 물체는 집기 불가
    const float Distance = FVector::Dist(
        GetActorLocation(),
        HitObject->GetActorLocation()
    );

    if (Distance > GrabDistance)
    {
        return;
    }

    // 집기 가능한 물체로 선택
    TargetActor = HitObject;
    TargetActor->SetHighlighted(true);
}

void AAGVRobot::GrabObject()
{
    if (IsValid(HeldActor))
    {
        return;
    }

    UpdateGrabTarget();

    if (!IsValid(TargetActor))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("No grabbable object nearby"));
        return;
    }

    AAGVGrabbableObject* ObjectToGrab = TargetActor;

    // 물체 집기
    ObjectToGrab->Grab(GrabPoint);

    if (ObjectToGrab->bIsGrabbed)
    {
        HeldActor = ObjectToGrab;
        TargetActor = nullptr;

        if (IsValid(PlacementPreview))
        {
            PlacementPreview->InitializeFromProduct(HeldActor);
        }

        UE_LOG(LogTemp, Warning,
            TEXT("Object grabbed successfully"));

        // 집은 상품이 진열대에 있던 상품인지 확인
        for (TActorIterator<AAGVDisplayShelf> It(GetWorld()); It; ++It)
        {
            AAGVDisplayShelf* Shelf = *It;

            if (IsValid(Shelf) &&
                Shelf->RemoveProductFromSlot(HeldActor))
            {
                UE_LOG(LogTemp, Log,
                    TEXT("Shelf slot updated after grabbing"));

                break;
            }
        }
    }
}

void AAGVRobot::ReleaseObject()
{
    if (!IsValid(HeldActor))
    {
        return;
    }

    // 우클릭 시점의 조준 상태 갱신
    UpdatePlacementPreview();

    if (IsValid(AimedShelf) &&
        AimedSlotIndex != INDEX_NONE)
    {
        if (AimedShelf->PlaceProductAtSlot(
            HeldActor,
            AimedSlotIndex))
        {
            HeldActor = nullptr;

            if (IsValid(PlacementPreview))
            {
                PlacementPreview->HidePreview();
            }

            AimedShelf = nullptr;
            AimedSlotIndex = INDEX_NONE;

            UE_LOG(LogTemp, Log,
                TEXT("Product stocked successfully"));

            return;
        }

        UE_LOG(LogTemp, Warning,
            TEXT("Cannot place product at aimed slot"));

        // 잘못된 슬롯이면 계속 들고 있음
        return;
    }

    // 진열대를 바라보지 않으면 바닥에 놓기
    HeldActor->Release();
    HeldActor = nullptr;

    if (IsValid(PlacementPreview))
    {
        PlacementPreview->HidePreview();
    }

    UE_LOG(LogTemp, Warning,
        TEXT("Object released with gravity"));
}



bool AAGVRobot::CanGrabObject() const
{
    return IsValid(TargetActor) && !IsValid(HeldActor);
}

bool AAGVRobot::IsHoldingObject() const
{
    return IsValid(HeldActor);
}

void AAGVRobot::UpdatePlacementPreview()
{
    AimedShelf = nullptr;
    AimedSlotIndex = INDEX_NONE;

    if (IsValid(PlacementPreview))
    {
        PlacementPreview->HidePreview();
    }

    // 상품을 들고 있지 않으면 미리보기 불필요
    if (!IsValid(HeldActor) ||
        !IsValid(PlacementPreview) ||
        !Camera ||
        !GetWorld())
    {
        return;
    }

    const FVector Start = Camera->GetComponentLocation();
    const FVector End =
        Start + Camera->GetForwardVector() * 1000.0f;

    FHitResult Hit;
    FCollisionQueryParams Params;
    Params.AddIgnoredActor(this);
    Params.AddIgnoredActor(HeldActor);

    const bool bHit = GetWorld()->LineTraceSingleByChannel(
        Hit,
        Start,
        End,
        ECC_Visibility,
        Params
    );

    if (!bHit)
    {
        return;
    }

    AAGVDisplayShelf* Shelf =
        Cast<AAGVDisplayShelf>(Hit.GetActor());

    if (!IsValid(Shelf))
    {
        return;
    }

    // AGV와 진열대 사이 거리 검사
    const float Distance = FVector::Dist2D(
        GetActorLocation(),
        Shelf->GetActorLocation()
    );

    if (Distance > 250.0f)
    {
        return;
    }

    // 조준 위치에서 가장 가까운 슬롯
    const int32 SlotIndex =
        Shelf->FindNearestSlot(Hit.ImpactPoint);

    if (SlotIndex == INDEX_NONE)
    {
        return;
    }

    AimedShelf = Shelf;
    AimedSlotIndex = SlotIndex;

    UE_LOG(LogTemp, Warning,
        TEXT("Aimed Shelf: %s | Slot: %d | ProductType: %d | ShelfType: %d | Occupied: %d"),
        *Shelf->GetName(),
        SlotIndex,
        static_cast<int32>(HeldActor->GetProductType()),
        static_cast<int32>(Shelf->GetShelfProductType()),
        Shelf->IsSlotOccupied(SlotIndex) ? 1 : 0
    );

    // 해당 슬롯에 진열 가능한지 검사
    const bool bCanPlace =
        HeldActor->GetProductType() == Shelf->GetShelfProductType()
        && !Shelf->IsSlotOccupied(SlotIndex);

    // 슬롯 위치로 미리보기 이동
    const FTransform SlotTransform(
        Shelf->GetActorRotation(),
        Shelf->GetSlotWorldLocation(SlotIndex)
    );

    PlacementPreview->UpdatePreview(
        SlotTransform,
        bCanPlace
    );
}



bool AAGVRobot::StartSimulationRoute(const FString& StartId, const FString& DestinationId)
{
    StopSimulationRoute();
    if (!IsValid(MapLoader))
    {
        for (TActorIterator<AWarehouseMapLoader> It(GetWorld()); It; ++It)
        {
            MapLoader = *It;
            break;
        }
    }
    if (!IsValid(MapLoader) || MapLoader->Nodes.IsEmpty())
    {
        UE_LOG(LogTemp, Warning, TEXT("Simulation: map not loaded yet"));
        return false;
    }
    if (!MapLoader->FindShortestRoute(StartId, DestinationId, RouteEdgeIndices, RouteReversed))
    {
        UE_LOG(LogTemp, Warning, TEXT("Simulation: route not found %s -> %s"), *StartId, *DestinationId);
        return false;
    }
    const FWarehouseNode* StartNode = MapLoader->Nodes.FindByPredicate(
        [&StartId](const FWarehouseNode& Node) { return Node.Id == StartId; });
    if (!StartNode) return false;

    SimulationHeight = GetActorLocation().Z;
    FVector SpawnPosition = StartNode->Position;
    SpawnPosition.Z = SimulationHeight;
    SetActorLocation(SpawnPosition, false);
    CurrentRouteStep = 0;
    CurrentEdgeDistance = 0.0f;
    bRouteActive = !RouteEdgeIndices.IsEmpty();
    if (bRouteActive && !PrepareSimulationEdge())
    {
        StopSimulationRoute();
        return false;
    }
    UE_LOG(LogTemp, Warning, TEXT("Simulation route started: %s -> %s (%d edges)"),
        *StartId, *DestinationId, RouteEdgeIndices.Num());
    return true;
}

bool AAGVRobot::StartServerRoute(
    const TArray<FString>& NodeIds,
    const FString& ReleasedUntil)
{
    if (MovementMode != EAGVMovementMode::Simulation)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("Server route ignored: AGV is not in Simulation mode"));
        return false;
    }

    if (NodeIds.Num() < 2)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("Server route requires at least two nodes"));
        return false;
    }

    if (!IsValid(MapLoader))
    {
        for (TActorIterator<AWarehouseMapLoader> It(GetWorld()); It; ++It)
        {
            MapLoader = *It;
            break;
        }
    }

    if (!IsValid(MapLoader) || MapLoader->Nodes.IsEmpty())
    {
        UE_LOG(LogTemp, Warning,
            TEXT("Server route: warehouse map not loaded"));
        return false;
    }

    // 허가된 마지막 노드 위치 확인
    const int32 LastAllowedNodeIndex =
        NodeIds.Find(ReleasedUntil);

    if (LastAllowedNodeIndex <= 0)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("Server route: invalid released_until %s"),
            *ReleasedUntil);
        return false;
    }

    // 이전에 받은 경로와 동일한지 확인
    const bool bSameRoute =
        bServerRouteReceived &&
        ServerRouteNodes == NodeIds;

    if (bSameRoute)
    {
        // 이전 허가 범위보다 작거나 같으면 무시
        if (LastAllowedNodeIndex <= ServerReleasedNodeIndex)
        {
            UE_LOG(LogTemp, Warning,
                TEXT("Server route: no new released nodes"));
            return true;
        }

        // 기존 경로에 추가로 허가된 Edge만 연결
        for (int32 i = ServerReleasedNodeIndex;
            i < LastAllowedNodeIndex; ++i)
        {
            int32 FoundEdge = INDEX_NONE;
            bool bReversed = false;

            for (int32 EdgeIndex = 0;
                EdgeIndex < MapLoader->Edges.Num(); ++EdgeIndex)
            {
                const FWarehouseEdge& Edge =
                    MapLoader->Edges[EdgeIndex];

                if (Edge.From == NodeIds[i] &&
                    Edge.To == NodeIds[i + 1])
                {
                    FoundEdge = EdgeIndex;
                    break;
                }

                if (Edge.From == NodeIds[i + 1] &&
                    Edge.To == NodeIds[i])
                {
                    FoundEdge = EdgeIndex;
                    bReversed = true;
                    break;
                }
            }

            if (FoundEdge == INDEX_NONE)
            {
                UE_LOG(LogTemp, Error,
                    TEXT("Server route: extension edge not found"));
                return false;
            }

            RouteEdgeIndices.Add(FoundEdge);
            RouteReversed.Add(bReversed);
        }

        ServerReleasedNodeIndex = LastAllowedNodeIndex;

        // J02 등에서 이미 정지한 상태라면 다음 Edge 준비
        if (!bRouteActive)
        {
            bRouteActive = true;

            if (!PrepareSimulationEdge())
            {
                bRouteActive = false;
                return false;
            }
        }

        UE_LOG(LogTemp, Warning,
            TEXT("Server route extended to %s"),
            *ReleasedUntil);

        return true;
    }

    TArray<int32> NewEdgeIndices;
    TArray<bool> NewReversed;

    // 서버가 허가한 노드까지만 Edge 생성
    for (int32 i = 0; i < LastAllowedNodeIndex; ++i)
    {
        const FString& FromId = NodeIds[i];
        const FString& ToId = NodeIds[i + 1];

        int32 FoundEdge = INDEX_NONE;
        bool bReversed = false;

        for (int32 EdgeIndex = 0;
            EdgeIndex < MapLoader->Edges.Num();
            ++EdgeIndex)
        {
            const FWarehouseEdge& Edge =
                MapLoader->Edges[EdgeIndex];

            if (Edge.From == FromId && Edge.To == ToId)
            {
                FoundEdge = EdgeIndex;
                bReversed = false;
                break;
            }

            if (Edge.From == ToId && Edge.To == FromId)
            {
                FoundEdge = EdgeIndex;
                bReversed = true;
                break;
            }
        }

        if (FoundEdge == INDEX_NONE)
        {
            UE_LOG(LogTemp, Error,
                TEXT("Server route: edge not found %s -> %s"),
                *FromId, *ToId);
            return false;
        }

        NewEdgeIndices.Add(FoundEdge);
        NewReversed.Add(bReversed);
    }

    const FWarehouseNode* StartNode =
        MapLoader->Nodes.FindByPredicate(
            [&NodeIds](const FWarehouseNode& Node)
            {
                return Node.Id == NodeIds[0];
            });

    if (!StartNode)
    {
        UE_LOG(LogTemp, Error,
            TEXT("Server route: start node not found"));
        return false;
    }

    StopSimulationRoute();

    RouteEdgeIndices = MoveTemp(NewEdgeIndices);
    RouteReversed = MoveTemp(NewReversed);

    SimulationHeight = GetActorLocation().Z;

    FVector StartPosition = StartNode->Position;
    StartPosition.Z = SimulationHeight;

    SetActorLocation(StartPosition, false);

    CurrentRouteStep = 0;
    CurrentEdgeDistance = 0.0f;

    bServerRouteReceived = true;
    bAutoRouteAttempted = true;
    bRouteActive = true;

    // 서버의 전체 경로와 현재 허가 범위 저장
    ServerRouteNodes = NodeIds;
    ServerReleasedNodeIndex = LastAllowedNodeIndex;
    ServerReachedNodeIndex = 0;

    if (!PrepareSimulationEdge())
    {
        StopSimulationRoute();
        return false;
    }

    UE_LOG(LogTemp, Warning,
        TEXT("Server route started: %s -> %s (%d edges)"),
        *NodeIds[0],
        *ReleasedUntil,
        RouteEdgeIndices.Num());

    return true;
}

void AAGVRobot::UpdateServerState(
    const FString& InStatus,
    const FString& InCurrentNode,
    float InBattery)
{
    ServerStatus = InStatus;
    ServerCurrentNode = InCurrentNode;
    ServerBattery = FMath::Clamp(InBattery, 0.0f, 100.0f);

    UE_LOG(LogTemp, Warning,
        TEXT("Server state updated: status=%s, node=%s, battery=%.1f"),
        *ServerStatus,
        *ServerCurrentNode,
        ServerBattery);
}

void AAGVRobot::UpdateExternalPosition(const FString& NodeId)
{
    if (MovementMode != EAGVMovementMode::External)
    {
        return;
    }

    if (!IsValid(MapLoader))
    {
        for (TActorIterator<AWarehouseMapLoader> It(GetWorld()); It; ++It)
        {
            MapLoader = *It;
            break;
        }
    }

    if (!IsValid(MapLoader) || MapLoader->Nodes.IsEmpty())
    {
        UE_LOG(LogTemp, Warning,
            TEXT("External: warehouse map not loaded"));
        return;
    }

    const FWarehouseNode* TargetNode =
        MapLoader->Nodes.FindByPredicate(
            [&NodeId](const FWarehouseNode& Node)
            {
                return Node.Id == NodeId;
            });

    if (!TargetNode)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("External: node not found %s"), *NodeId);
        return;
    }

    FVector TargetPosition = TargetNode->Position;

    // AGV의 현재 높이는 유지
    TargetPosition.Z = GetActorLocation().Z;

    SetActorLocation(TargetPosition, false);

    UE_LOG(LogTemp, Warning,
        TEXT("External position updated: %s"), *NodeId);
}

void AAGVRobot::UpdateExternalPose(
    float X,
    float Y,
    float Yaw)
{
    if (MovementMode != EAGVMovementMode::External)
    {
        return;
    }

    // 실제 서버 좌표 수신 시에만 타이머 초기화
    ExternalDataElapsed = 0.0f;
    bExternalDataReceived = true;

    if (bExternalDataStale)
    {
        bExternalDataStale = false;

        UE_LOG(
            LogTemp,
            Warning,
            TEXT("[External] Data connection restored")
        );
    }

    // 현재 위치를 새로운 보간 시작점으로 설정
    ExternalStartPosition = GetActorLocation();

    // 서버 좌표를 Unreal 좌표로 변환
    ExternalTargetPosition = FVector(
        X * ExternalPositionScale,
        Y * ExternalPositionScale,
        GetActorLocation().Z
    );

    ExternalStartRotation = GetActorRotation();
    ExternalTargetRotation = FRotator(0.0f, Yaw, 0.0f);

    // 첫 위치 즉시 설정
    if (!bExternalPoseInitialized)
    {
        SetActorLocation(ExternalTargetPosition, false);
        SetActorRotation(ExternalTargetRotation);

        bExternalPoseInitialized = true;
        bExternalInterpolating = false;
        return;
    }

    // 현재 위치와 새 목표 위치의 차이
    const float PositionDifference = FVector::Dist2D(
        GetActorLocation(),
        ExternalTargetPosition
    );

    // 현재 방향과 새 목표 방향의 차이
    const float RotationDifference = FMath::Abs(
        FMath::FindDeltaAngleDegrees(
            GetActorRotation().Yaw,
            ExternalTargetRotation.Yaw
        )
    );

    // 위치와 방향 모두 동일하면 새로운 보간을 시작하지 않음
    if (PositionDifference <= ExternalPositionTolerance &&
        RotationDifference <= ExternalRotationTolerance)
    {
        // 이미 진행 중인 보간이 있으면 중단하고 목표에 정렬
        SetActorLocation(ExternalTargetPosition);
        SetActorRotation(ExternalTargetRotation);

        bExternalRotating = false;
        bExternalInterpolating = false;

        return;
    }

    // 새로운 목표를 받으면 회전 단계부터 시작
    ExternalRotationElapsed = 0.0f;
    ExternalInterpolationElapsed = 0.0f;

    // 현재 위치에서 목표 위치까지 거리 (cm)
    const float MoveDistance = FVector::Dist2D(
        ExternalStartPosition,
        ExternalTargetPosition
    );

    // 거리에 비례해 이동 시간 계산
    ExternalCurrentMoveDuration = FMath::Max(
        MoveDistance / FMath::Max(ExternalMoveSpeed, 1.0f),
        ExternalMinInterpolationDuration
    );

    // 현재 방향과 목표 방향이 다르면 먼저 제자리 회전
    const float YawDifference = FMath::Abs(
        FMath::FindDeltaAngleDegrees(
            ExternalStartRotation.Yaw,
            ExternalTargetRotation.Yaw
        )
    );

    bExternalRotating = YawDifference > 1.0f;

    // 회전이 필요 없으면 바로 이동
    bExternalInterpolating = !bExternalRotating;

    UE_LOG(LogTemp, Warning,
        TEXT("External target received: X=%.2f, Y=%.2f, Yaw=%.2f"),
        X, Y, Yaw);
}

void AAGVRobot::UpdateExternalInterpolation(float DeltaTime)
{
    if (MovementMode != EAGVMovementMode::External)
    {
        return;
    }

    // 1단계: 제자리 회전
    if (bExternalRotating)
    {
        ExternalRotationElapsed += DeltaTime;

        const float RotationDuration =
            FMath::Max(ExternalRotationDuration, 0.01f);

        const float RotationAlpha = FMath::Clamp(
            ExternalRotationElapsed / RotationDuration,
            0.0f,
            1.0f
        );

        // 최단 회전 방향으로 회전
        const FQuat NewRotation = FQuat::Slerp(
            ExternalStartRotation.Quaternion(),
            ExternalTargetRotation.Quaternion(),
            RotationAlpha
        );

        SetActorRotation(NewRotation);

        if (RotationAlpha >= 1.0f)
        {
            bExternalRotating = false;
            bExternalInterpolating = true;
            ExternalInterpolationElapsed = 0.0f;

            UE_LOG(LogTemp, Warning,
                TEXT("External rotation completed"));
        }

        return;
    }

    // 2단계: 직진 이동
    if (bExternalInterpolating)
    {
        ExternalInterpolationElapsed += DeltaTime;

        const float Duration =
            FMath::Max(ExternalCurrentMoveDuration, 0.01f);

        const float Alpha = FMath::Clamp(
            ExternalInterpolationElapsed / Duration,
            0.0f,
            1.0f
        );

        const FVector NewPosition = FMath::Lerp(
            ExternalStartPosition,
            ExternalTargetPosition,
            Alpha
        );

        SetActorLocation(NewPosition, false);

        if (Alpha >= 1.0f)
        {
            bExternalInterpolating = false;

            UE_LOG(LogTemp, Warning,
                TEXT("External target reached"));
        }
    }
}

void AAGVRobot::StopSimulationRoute()
{
    bRouteActive = false;
    RouteEdgeIndices.Reset();
    RouteReversed.Reset();
    CurrentRouteStep = 0;
    CurrentEdgeDistance = 0.0f;
}

bool AAGVRobot::PrepareSimulationEdge()
{
    if (!IsValid(MapLoader) || !SimulationSpline ||
        !RouteEdgeIndices.IsValidIndex(CurrentRouteStep) ||
        !RouteReversed.IsValidIndex(CurrentRouteStep)) return false;

    const FWarehouseEdge& Edge = MapLoader->Edges[RouteEdgeIndices[CurrentRouteStep]];
    const FWarehouseNode* From = MapLoader->Nodes.FindByPredicate(
        [&Edge](const FWarehouseNode& Node) { return Node.Id == Edge.From; });
    const FWarehouseNode* To = MapLoader->Nodes.FindByPredicate(
        [&Edge](const FWarehouseNode& Node) { return Node.Id == Edge.To; });
    if (!From || !To) return false;

    TArray<FVector> Points;
    Points.Add(From->Position);
    Points.Append(Edge.Points);
    Points.Add(To->Position);
    if (RouteReversed[CurrentRouteStep]) Algo::Reverse(Points);

    SimulationSpline->ClearSplinePoints(false);
    for (FVector Point : Points)
    {
        Point.Z = SimulationHeight;
        SimulationSpline->AddSplinePoint(Point, ESplineCoordinateSpace::World, false);
    }
    // 직선 구간은 Linear, 곡선 경유점이 있으면 Curve
    if (Points.Num() == 2)
    {
        SimulationSpline->SetSplinePointType(0, ESplinePointType::Linear, false);
        SimulationSpline->SetSplinePointType(1, ESplinePointType::Linear, false);
    }
    SimulationSpline->UpdateSpline();
    CurrentEdgeDistance = 0.0f;
    return true;
}

void AAGVRobot::UpdateSimulationMovement(float DeltaTime)
{
    if (!bRouteActive)
    {
        if (bAutoStartSimulationRoute &&
            !bAutoRouteAttempted &&
            !bServerRouteReceived)
        {
            // Loader의 Blueprint BeginPlay에서 LoadMap()을 호출하므로
            // 실제 데이터가 준비된 프레임에 자동 시작한다.
            for (TActorIterator<AWarehouseMapLoader> It(GetWorld()); It; ++It)
            {
                MapLoader = *It;
                break;
            }
            if (IsValid(MapLoader) && !MapLoader->Nodes.IsEmpty())
            {
                bAutoRouteAttempted = true;
                StartSimulationRoute(SimulationStartNode, SimulationDestinationNode);
            }
        }
        return;
    }

    float RemainingMove = FMath::Max(0.0f, SimulationSpeed) * DeltaTime;
    // 긴 프레임에도 여러 Edge를 연속 통과할 수 있도록 처리
    while (bRouteActive && RemainingMove > KINDA_SMALL_NUMBER)
    {
        const float EdgeLength = SimulationSpline->GetSplineLength();
        const float RemainingEdge = FMath::Max(0.0f, EdgeLength - CurrentEdgeDistance);
        const float Step = FMath::Min(RemainingMove, RemainingEdge);
        const float NextDistance = CurrentEdgeDistance + Step;
        const FVector Target = SimulationSpline->GetLocationAtDistanceAlongSpline(
            NextDistance, ESplineCoordinateSpace::World);
        const FVector Direction = SimulationSpline->GetDirectionAtDistanceAlongSpline(
            NextDistance, ESplineCoordinateSpace::World);

        FHitResult Hit;
        SetActorLocation(Target, bSimulationSweep, &Hit);
        if (bSimulationSweep && Hit.bBlockingHit)
        {
            UE_LOG(LogTemp, Warning, TEXT("Simulation blocked by collision: %s"),
                *GetNameSafe(Hit.GetActor()));
            StopSimulationRoute();
            return;
        }
        if (!Direction.IsNearlyZero())
        {
            FRotator Desired = Direction.Rotation();
            Desired.Pitch = 0.0f;
            Desired.Roll = 0.0f;
            SetActorRotation(Desired);
            if (Controller) Controller->SetControlRotation(FRotator(
                Controller->GetControlRotation().Pitch, Desired.Yaw, 0.0f));
        }
        CurrentEdgeDistance = NextDistance;
        RemainingMove -= Step;
        if (CurrentEdgeDistance + 0.01f >= EdgeLength)
        {
            ++CurrentRouteStep;

            if (bServerRouteReceived)
            {
                ServerReachedNodeIndex = CurrentRouteStep;
            }

            if (CurrentRouteStep >= RouteEdgeIndices.Num())
            {
                if (bServerRouteReceived)
                {
                    // 서버 허가 구간 끝에서 대기
                    bRouteActive = false;

                    UE_LOG(LogTemp, Warning,
                        TEXT("Server route: waiting at %s"),
                        ServerRouteNodes.IsValidIndex(ServerReachedNodeIndex)
                        ? *ServerRouteNodes[ServerReachedNodeIndex]
                        : TEXT("Unknown"));
                    
                    return;
                }

                // 기존 자동 시뮬레이션은 종전 방식 유지
                UE_LOG(LogTemp, Warning,
                    TEXT("Simulation destination reached"));

                StopSimulationRoute();
                return;
            }
            if (!PrepareSimulationEdge())
            {
                StopSimulationRoute();
                return;
            }
        }
        else if (Step <= KINDA_SMALL_NUMBER) break;
    }
}

void AAGVRobot::DrawPlannedRoute() const
{
    if (!bShowPlannedRoute || !GetWorld())
    {
        return;
    }

    // 현재 AGV가 참조하는 WarehouseMapLoader
    // MapLoader는 실제 AGVRobot 코드의 변수명에 맞춰 사용
    if (!IsValid(MapLoader))
    {
        return;
    }

    const FColor LineColor = PlannedRouteColor.ToFColor(true);

    // 현재 최단 경로에 포함된 Edge 순회
    for (int32 EdgeIndex : RouteEdgeIndices)
    {
        if (!MapLoader->SpawnedPaths.IsValidIndex(EdgeIndex))
        {
            continue;
        }

        AAGVPathActor* PathActor =
            MapLoader->SpawnedPaths[EdgeIndex];

        if (!IsValid(PathActor))
        {
            continue;
        }

        USplineComponent* Spline =
            PathActor->FindComponentByClass<USplineComponent>();

        if (!Spline)
        {
            continue;
        }

        const float Length = Spline->GetSplineLength();

        // 20cm 간격으로 Spline을 샘플링
        const int32 SegmentCount =
            FMath::Max(1, FMath::CeilToInt(Length / 20.0f));

        for (int32 i = 0; i < SegmentCount; ++i)
        {
            const float StartDistance =
                Length * i / SegmentCount;

            const float EndDistance =
                Length * (i + 1) / SegmentCount;

            FVector Start = Spline->GetLocationAtDistanceAlongSpline(
                StartDistance,
                ESplineCoordinateSpace::World);

            FVector End = Spline->GetLocationAtDistanceAlongSpline(
                EndDistance,
                ESplineCoordinateSpace::World);

            Start.Z += PlannedRouteZOffset;
            End.Z += PlannedRouteZOffset;

            DrawDebugLine(
                GetWorld(),
                Start,
                End,
                LineColor,
                false,   // 매 프레임 새로 그림
                0.0f,
                0,
                PlannedRouteThickness
            );
        }
    }
}

void AAGVRobot::ResetExternalPoseInitialization()
{
    bExternalPoseInitialized = false;

    bExternalRotating = false;
    bExternalInterpolating = false;

    ExternalRotationElapsed = 0.0f;
    ExternalInterpolationElapsed = 0.0f;

    UE_LOG(LogTemp, Warning,
        TEXT("[External] Pose initialization reset"));
}