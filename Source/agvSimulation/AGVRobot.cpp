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

bool AAGVRobot::ApplyExternalGrabSuccess(EProductType ProductType)
{
    if (MovementMode != EAGVMovementMode::External)
    {
        return false;
    }

    if (!GetWorld() || !IsValid(GrabPoint))
    {
        return false;
    }

    // 이미 물체를 들고 있으면 중복 집기 방지
    if (IsValid(HeldActor))
    {
        return false;
    }

    AAGVGrabbableObject* NearestObject = nullptr;
    float NearestDistance = TNumericLimits<float>::Max();

    // 레벨에 존재하는 물체 검색
    for (TActorIterator<AAGVGrabbableObject> It(GetWorld()); It; ++It)
    {
        AAGVGrabbableObject* Candidate = *It;

        if (!IsValid(Candidate))
        {
            continue;
        }

        // 이미 집힌 물체 제외
        if (Candidate->bIsGrabbed)
        {
            continue;
        }

        // 실제 AGV가 인식한 색상과 비교
        if (Candidate->GetProductType() != ProductType)
        {
            continue;
        }

        const float Distance = FVector::Dist2D(
            GetActorLocation(),
            Candidate->GetActorLocation()
        );

        // 집기 가능 범위 내에서 가장 가까운 물체 선택
        if (Distance <= GrabDistance &&
            Distance < NearestDistance)
        {
            NearestDistance = Distance;
            NearestObject = Candidate;
        }
    }

    if (!IsValid(NearestObject))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] GRAB FAILED: Type=%d, AGVPos=%s, GrabDistance=%.1f"),
            *GetAGVId(),
            static_cast<int32>(ProductType),
            *GetActorLocation().ToString(),
            GrabDistance
        );

        return false;
    }

    // 실제 집기 성공 결과 반영
    NearestObject->Grab(GrabPoint);

    if (!NearestObject->bIsGrabbed)
    {
        return false;
    }

    HeldActor = NearestObject;

    // 진열대 슬롯 상태 갱신
    for (TActorIterator<AAGVDisplayShelf> It(GetWorld()); It; ++It)
    {
        AAGVDisplayShelf* Shelf = *It;

        if (IsValid(Shelf) &&
            Shelf->RemoveProductFromSlot(HeldActor))
        {
            break;
        }
    }

    UE_LOG(LogTemp, Log,
        TEXT("[%s] External grab success: Type=%d"),
        *GetAGVId(),
        static_cast<int32>(ProductType));

    return true;
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

bool AAGVRobot::ApplyExternalReleaseSuccess()
{
    if (MovementMode != EAGVMovementMode::External)
    {
        return false;
    }

    if (!IsValid(HeldActor))
    {
        return false;
    }

    HeldActor->Release();

    if (HeldActor->bIsGrabbed)
    {
        return false;
    }

    HeldActor = nullptr;

    if (IsValid(PlacementPreview))
    {
        PlacementPreview->HidePreview();
    }

    AimedShelf = nullptr;
    AimedSlotIndex = INDEX_NONE;

    UE_LOG(LogTemp, Log,
        TEXT("[%s] External release success"),
        *GetAGVId());

    return true;
}

bool AAGVRobot::ApplyExternalPlaceSuccess(
    AAGVDisplayShelf* TargetShelf,
    int32 SlotIndex
)
{
    UE_LOG(LogTemp, Warning,
        TEXT("[%s] PLACE EVENT RECEIVED: Shelf=%s, Slot=%d"),
        *GetAGVId(),
        IsValid(TargetShelf)
        ? *TargetShelf->GetShelfId()
        : TEXT("NONE"),
        SlotIndex);

    // 외부 데이터 연동 모드 확인
    if (MovementMode != EAGVMovementMode::External)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] PLACE FAILED: Not External Mode"),
            *GetAGVId());

        return false;
    }

    // 상품을 들고 있는지 확인
    if (!IsValid(HeldActor))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] PLACE FAILED: HeldActor is NULL"),
            *GetAGVId());

        return false;
    }

    // 대상 선반 확인
    if (!IsValid(TargetShelf))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] PLACE FAILED: TargetShelf is NULL"),
            *GetAGVId());

        return false;
    }

    // 상품 종류 확인
    if (HeldActor->GetProductType() !=
        TargetShelf->GetShelfProductType())
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] PLACE FAILED: Product Type Mismatch"),
            *GetAGVId());

        return false;
    }

    // 슬롯 점유 확인
    if (TargetShelf->IsSlotOccupied(SlotIndex))
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] PLACE FAILED: Slot %d Occupied"),
            *GetAGVId(),
            SlotIndex);

        return false;
    }

    // 선반에 상품 적재
    const bool bPlaced =
        TargetShelf->PlaceProductAtSlot(
            HeldActor,
            SlotIndex
        );

    if (!bPlaced)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[%s] PLACE FAILED: PlaceProductAtSlot returned false"),
            *GetAGVId());

        return false;
    }

    // 적재 성공
    HeldActor = nullptr;

    if (IsValid(PlacementPreview))
    {
        PlacementPreview->HidePreview();
    }

    AimedShelf = nullptr;
    AimedSlotIndex = INDEX_NONE;

    UE_LOG(LogTemp, Warning,
        TEXT("[%s] PLACE SUCCESS: Shelf=%s, Slot=%d"),
        *GetAGVId(),
        *TargetShelf->GetShelfId(),
        SlotIndex);

    return true;
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

    const FVector NewTargetPosition(
        X * ExternalPositionScale,
        Y * ExternalPositionScale,
        GetActorLocation().Z
    );

    const FRotator NewTargetRotation(
        0.0f,
        Yaw,
        0.0f
    );

    // 첫 좌표 수신 시 즉시 초기 위치 설정
    if (!bExternalPoseInitialized)
    {
        SetActorLocation(NewTargetPosition, false);
        SetActorRotation(NewTargetRotation);

        ExternalTargetPosition = NewTargetPosition;
        ExternalTargetRotation = NewTargetRotation;

        bExternalPoseInitialized = true;
        bExternalRotating = false;
        bExternalInterpolating = false;

        return;
    }

    // 새로운 JSON 좌표를 목표 위치로 갱신
    ExternalTargetPosition = NewTargetPosition;
    ExternalTargetRotation = NewTargetRotation;

    // 기존 회전 후 이동 방식 비활성화
    bExternalRotating = false;

    // 이동 및 회전을 Tick에서 함께 처리
    bExternalInterpolating = true;
}

void AAGVRobot::UpdateExternalInterpolation(float DeltaTime)
{
    if (MovementMode != EAGVMovementMode::External)
    {
        return;
    }

    if (!bExternalPoseInitialized || !bExternalDataReceived)
    {
        return;
    }

    // 통신이 끊겼으면 마지막 위치 유지
    if (bExternalDataStale)
    {
        return;
    }

    const FVector CurrentPosition = GetActorLocation();
    const FRotator CurrentRotation = GetActorRotation();

    // 위치 추종 속도
    constexpr float PositionInterpSpeed = 15.0f;

    // 회전 추종 속도
    constexpr float RotationInterpSpeed = 15.0f;

    // 목표 위치로 부드럽게 이동
    const FVector NewPosition = FMath::VInterpTo(
        CurrentPosition,
        ExternalTargetPosition,
        DeltaTime,
        PositionInterpSpeed
    );

    // 목표 방향으로 부드럽게 회전
    const FRotator NewRotation = FMath::RInterpTo(
        CurrentRotation,
        ExternalTargetRotation,
        DeltaTime,
        RotationInterpSpeed
    );

    SetActorLocation(NewPosition, false);
    SetActorRotation(NewRotation);

    const float PositionDifference = FVector::Dist2D(
        NewPosition,
        ExternalTargetPosition
    );

    const float RotationDifference = FMath::Abs(
        FMath::FindDeltaAngleDegrees(
            NewRotation.Yaw,
            ExternalTargetRotation.Yaw
        )
    );

    // 충분히 가까워지면 정확한 목표 좌표로 정렬
    if (PositionDifference <= 0.5f &&
        RotationDifference <= 0.5f)
    {
        SetActorLocation(ExternalTargetPosition, false);
        SetActorRotation(ExternalTargetRotation);

        bExternalInterpolating = false;
    }
    else
    {
        bExternalInterpolating = true;
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