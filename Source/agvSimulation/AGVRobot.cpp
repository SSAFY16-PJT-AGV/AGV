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
        // TODO: 중앙 서버에서 수신한 AGV 위치 반영
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
        if (!bAutoRouteAttempted)
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
            if (CurrentRouteStep >= RouteEdgeIndices.Num())
            {
                UE_LOG(LogTemp, Warning, TEXT("Simulation destination reached"));
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