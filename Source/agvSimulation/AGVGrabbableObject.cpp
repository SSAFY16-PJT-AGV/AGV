
#include "AGVGrabbableObject.h"

#include "Components/StaticMeshComponent.h"
#include "Components/SceneComponent.h"
#include "UObject/ConstructorHelpers.h"
#include "DrawDebugHelpers.h"

AAGVGrabbableObject::AAGVGrabbableObject()
{
    PrimaryActorTick.bCanEverTick = true;

    // 물체 메시 생성
    ObjectMesh = CreateDefaultSubobject<UStaticMeshComponent>(
        TEXT("ObjectMesh"));

    RootComponent = ObjectMesh;

    // 기본 큐브 메시
    static ConstructorHelpers::FObjectFinder<UStaticMesh> CubeMesh(
        TEXT("/Engine/BasicShapes/Cube.Cube"));

    if (CubeMesh.Succeeded())
    {
        ObjectMesh->SetStaticMesh(CubeMesh.Object);
    }

    ObjectMesh->SetRelativeScale3D(
        FVector(0.5f, 0.5f, 0.5f));

    ObjectMesh->SetMobility(EComponentMobility::Movable);

    // 충돌 및 물리
    ObjectMesh->SetCollisionEnabled(
        ECollisionEnabled::QueryAndPhysics);

    ObjectMesh->SetCollisionObjectType(ECC_WorldDynamic);
    ObjectMesh->SetCollisionResponseToAllChannels(ECR_Block);

    ObjectMesh->SetGenerateOverlapEvents(true);

    ObjectMesh->SetSimulatePhysics(false);
    ObjectMesh->SetEnableGravity(true);

    // 집기 가능 태그
    Tags.Add(TEXT("Grabbable"));

    // 강조 효과 초기 비활성화
    ObjectMesh->SetRenderCustomDepth(false);
    ObjectMesh->SetCustomDepthStencilValue(1);
}

void AAGVGrabbableObject::Grab(USceneComponent* AttachPoint)
{
    if (!IsValid(AttachPoint) || bIsGrabbed)
    {
        return;
    }

    ObjectMesh->SetSimulatePhysics(false);

    ObjectMesh->SetCollisionEnabled(
        ECollisionEnabled::NoCollision);

    const bool bAttached = AttachToComponent(
        AttachPoint,
        FAttachmentTransformRules::SnapToTargetNotIncludingScale);

    if (!bAttached)
    {
        ObjectMesh->SetCollisionEnabled(
            ECollisionEnabled::QueryAndPhysics);
        return;
    }

    bIsGrabbed = true;
    SetHighlighted(false);
}

void AAGVGrabbableObject::Release()
{
    if (!bIsGrabbed)
    {
        return;
    }

    DetachFromActor(
        FDetachmentTransformRules::KeepWorldTransform);

    ObjectMesh->SetCollisionEnabled(
        ECollisionEnabled::QueryAndPhysics);

    ObjectMesh->SetEnableGravity(true);
    ObjectMesh->SetSimulatePhysics(true);
    ObjectMesh->WakeAllRigidBodies();

    bIsGrabbed = false;
}

void AAGVGrabbableObject::SetHighlighted(bool bHighlighted)
{
    bIsHighlighted = bHighlighted;
}


EProductType AAGVGrabbableObject::GetProductType() const
{
    return ProductType;
}

void AAGVGrabbableObject::Tick(float DeltaTime)
{
    Super::Tick(DeltaTime);

    if (!bIsHighlighted || bIsGrabbed)
    {
        return;
    }

    // 큐브 중심 기준 아래쪽에 링 배치
    const FVector RingLocation =
        GetActorLocation() + FVector(0.0f, 0.0f, -23.0f);

    DrawDebugCircle(
        GetWorld(),
        RingLocation,
        HighlightRadius,
        48,
        FColor::Cyan,
        false,
        0.0f,
        0,
        4.0f,
        FVector(1.0f, 0.0f, 0.0f),
        FVector(0.0f, 1.0f, 0.0f),
        false
    );
}
